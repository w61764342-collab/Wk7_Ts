import requests
from bs4 import BeautifulSoup
import os
import boto3
from botocore.exceptions import NoCredentialsError
import time
import logging
from urllib.parse import urljoin, quote
import re
import ssl
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import pandas as pd
from io import BytesIO
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Custom SSL adapter to handle legacy SSL renegotiation
class SSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.load_default_certs()
        context.set_ciphers('DEFAULT@SECLEVEL=1')
        # Allow legacy renegotiation for older servers
        context.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

# Disable SSL warnings for legacy connections
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class KCSBScraper:
    def __init__(self, aws_access_key, aws_secret_key, bucket_name, max_workers=5):
        self.base_url = "https://www.csb.gov.kw/Pages/"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # Mount custom SSL adapter for both http and https
        self.session.mount('https://', SSLAdapter())
        self.session.mount('http://', SSLAdapter())
        
        # S3 setup
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key
        )
        self.bucket_name = bucket_name
        self.base_s3_path = "KCSB-Data"
        self.max_workers = max_workers
        self.s3_lock = Lock()
        self.session_lock = Lock()
        self.request_count = 0
        self.request_lock = Lock()
        
        # S3 cache to reduce redundant checks
        self.s3_exists_cache = {}
        
    def sanitize_filename(self, filename):
        """Remove or replace invalid characters for file names"""
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = filename.strip()
        return filename
    
    def get_categories(self):
        """Extract all main categories and subcategories with retry logic"""
        url = f"{self.base_url}Statistics.aspx?ID=18&ParentCatID=2"
        
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Fetching categories (attempt {attempt}/{max_retries})...")
                response = self.session.get(url, timeout=60)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'html.parser')
                break  # Success, exit retry loop
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries:
                    wait_time = attempt * 3  # Exponential backoff: 3s, 6s, 9s
                    logger.warning(f"Timeout fetching categories, retry {attempt}/{max_retries} in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Error fetching categories after {max_retries} retries: {e}")
                    return []
            except Exception as e:
                logger.error(f"Error fetching categories: {e}")
                return []
        
        try:
            categories = []
            
            # Find all toggle sections (main categories)
            toggle_sections = soup.find_all('div', class_='toggle')
            
            for section in toggle_sections:
                label = section.find('label')
                if not label:
                    continue
                    
                main_category = label.get_text(strip=True)
                toggle_content = section.find('div', class_='toggle-content')
                
                if not toggle_content:
                    continue
                
                # Find all subcategories
                links = toggle_content.find_all('a', href=True)
                
                for link in links:
                    # Skip parent links without IDs
                    if link.get('class') and 'parent' in link.get('class'):
                        continue
                        
                    subcategory_name = link.find('span').get_text(strip=True) if link.find('span') else link.get_text(strip=True)
                    href = link['href']
                    
                    # Extract ID and ParentCatID from href
                    id_match = re.search(r'ID=(\d+)', href)
                    parent_match = re.search(r'ParentCatID=(\d+)', href)
                    
                    if id_match:
                        category_id = id_match.group(1)
                        parent_id = parent_match.group(1) if parent_match else ''
                        
                        categories.append({
                            'main_category': main_category,
                            'subcategory': subcategory_name,
                            'id': category_id,
                            'parent_id': parent_id,
                            'url': urljoin(self.base_url, href.replace('Statistics.aspx', 'Statistics'))
                        })
            
            logger.info(f"Found {len(categories)} subcategories across all main categories")
            return categories
            
        except Exception as e:
            logger.error(f"Error parsing categories: {e}")
            return []
    
    def get_viewstate_data(self, soup):
        """Extract ASP.NET ViewState and other hidden fields"""
        viewstate = soup.find('input', {'name': '__VIEWSTATE'})
        viewstate_gen = soup.find('input', {'name': '__VIEWSTATEGENERATOR'})
        event_validation = soup.find('input', {'name': '__EVENTVALIDATION'})
        
        return {
            '__VIEWSTATE': viewstate['value'] if viewstate else '',
            '__VIEWSTATEGENERATOR': viewstate_gen['value'] if viewstate_gen else '',
            '__EVENTVALIDATION': event_validation['value'] if event_validation else ''
        }
    
    def scrape_tab_content(self, category_url, tab_name, tab_id):
        """Scrape content from a specific tab with retry logic"""
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # Add small delay to avoid overwhelming server
                with self.request_lock:
                    self.request_count += 1
                    if self.request_count % 10 == 0:
                        time.sleep(1)  # Brief pause every 10 requests
                
                response = self.session.get(category_url, timeout=60)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'html.parser')
                break  # Success, exit retry loop
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries:
                    wait_time = attempt * 2  # Exponential backoff: 2s, 4s, 6s
                    logger.warning(f"Timeout on tab {tab_name}, retry {attempt}/{max_retries} in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Error scraping tab {tab_name} after {max_retries} retries: {e}")
                    return {'files': [], 'text_content': None}
            except Exception as e:
                logger.error(f"Error scraping tab {tab_name}: {e}")
                return {'files': [], 'text_content': None}
        
        try:
            # Get the tab content div
            tab_content = soup.find('div', {'id': tab_id})
            
            if not tab_content:
                logger.warning(f"Tab {tab_name} not found")
                return {'files': [], 'text_content': None}
            
            files = []
            text_content = None
            
            # Find table with files
            table = tab_content.find('table')
            
            if table:
                rows = table.find('tbody').find_all('tr') if table.find('tbody') else []
                
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) < 2:
                        continue
                    
                    title = cols[0].get_text(strip=True)
                    
                    # Check if this row has a modal trigger (skip these, get files from modal instead)
                    title_cell = cols[0]
                    modal_trigger = title_cell.find('a', {'data-toggle': 'modal'}) or title_cell.find('a', {'onclick': lambda x: x and 'modal' in x.lower()})
                    
                    if modal_trigger:
                        logger.debug(f"    Skipping parent row (opens modal): {title[:50]}")
                        continue
                    
                    # Find download links (only with file icons)
                    pdf_links = cols[1].find_all('a', href=True)
                    
                    for link in pdf_links:
                        img = link.find('img')
                        if not img:
                            continue
                        
                        img_src = img.get('src', '')
                        
                        # Only process actual file download links (with pdf/excel icons)
                        if 'pdf' not in img_src.lower() and 'xls' not in img_src.lower():
                            continue
                        
                        file_type = 'pdf' if 'pdf' in img_src.lower() else 'excel' if 'xls' in img_src.lower() else 'unknown'
                        
                        # Extract the postback event target
                        href = link.get('href', '')
                        
                        # Must contain __doPostBack to be a valid download link
                        if '__doPostBack' not in href:
                            logger.debug(f"    Skipping non-postback link: {title[:30]}")
                            continue
                        
                        event_target_match = re.search(r"'([^']+)'", href)
                        
                        if event_target_match:
                            event_target = event_target_match.group(1)
                            
                            files.append({
                                'title': title,
                                'file_type': file_type,
                                'event_target': event_target
                            })
            
            # Also check for modal popup with additional files
            modal = soup.find('div', {'id': 'Panel_Statistic'})
            if modal:
                logger.info(f"    Found modal popup with additional files")
                modal_table = modal.find('table')
                
                if modal_table:
                    modal_rows = modal_table.find('tbody').find_all('tr') if modal_table.find('tbody') else []
                    
                    for row in modal_rows:
                        cols = row.find_all('td')
                        if len(cols) < 2:
                            continue
                        
                        title = cols[0].get_text(strip=True)
                        
                        # Find download links (only with file icons)
                        pdf_links = cols[1].find_all('a', href=True)
                        
                        for link in pdf_links:
                            img = link.find('img')
                            if not img:
                                continue
                            
                            img_src = img.get('src', '')
                            
                            # Only process actual file download links (with pdf/excel icons)
                            if 'pdf' not in img_src.lower() and 'xls' not in img_src.lower():
                                continue
                            
                            file_type = 'pdf' if 'pdf' in img_src.lower() else 'excel' if 'xls' in img_src.lower() else 'unknown'
                            
                            # Extract the postback event target
                            href = link.get('href', '')
                            
                            # Must contain __doPostBack to be a valid download link
                            if '__doPostBack' not in href:
                                logger.debug(f"    Skipping non-postback modal link: {title[:30]}")
                                continue
                            
                            event_target_match = re.search(r"'([^']+)'", href)
                            
                            if event_target_match:
                                event_target = event_target_match.group(1)
                                
                                files.append({
                                    'title': title,
                                    'file_type': file_type,
                                    'event_target': event_target,
                                    'source': 'modal'  # Mark as coming from modal
                                })
            
            # If no files found, check for text content
            if not files:
                text_content = self.extract_text_content(tab_content, tab_id)
            
            return {'files': files, 'text_content': text_content}
                
        except Exception as e:
            logger.error(f"Error processing tab content {tab_name}: {e}")
            return {'files': [], 'text_content': None}
    
    def extract_text_content(self, tab_content, tab_id):
        """Extract text content from tabs like الموضوع, البيانات الوصفية, التقارير"""
        data = {}
        
        try:
            # For T2 (الموضوع) - extract definition and components
            if tab_id == 'T2':
                list_group = tab_content.find('div', class_='list-group')
                if list_group:
                    sections = []
                    list_items = list_group.find_all('a', class_='list-group-item')
                    
                    current_section = None
                    for item in list_items:
                        if 'active' in item.get('class', []):
                            current_section = item.get_text(strip=True)
                        else:
                            content = item.get_text(strip=True)
                            if content and current_section:
                                sections.append({
                                    'القسم': current_section,
                                    'المحتوى': content
                                })
                    
                    if sections:
                        data['sections'] = sections
            
            # For T4 (البيانات الوصفية) - extract metadata
            elif tab_id == 'T4':
                title_elem = tab_content.find('span', {'id': re.compile(r'.*lbl_calc_title.*')})
                details_elem = tab_content.find('span', {'id': re.compile(r'.*lbl_calc_details.*')})
                
                title = title_elem.get_text(strip=True) if title_elem else ''
                details = details_elem.get_text(strip=True) if details_elem else ''
                
                if title or details:
                    data['metadata'] = [{
                        'العنوان': title,
                        'التفاصيل': details
                    }]
            
            # For T5 (التقارير) - check for any text content
            elif tab_id == 'T5':
                # Sometimes T5 has text content outside the table
                text_divs = tab_content.find_all('div', class_='col-md-12')
                content_found = []
                
                for div in text_divs:
                    text = div.get_text(strip=True)
                    # Filter out empty or very short text
                    if text and len(text) > 50:
                        content_found.append(text)
                
                if content_found:
                    data['reports'] = [{'المحتوى': '\n\n'.join(content_found)}]
            
            return data if data else None
            
        except Exception as e:
            logger.error(f"Error extracting text content: {e}")
            return None
    
    def create_excel_from_data(self, data, tab_name):
        """Convert text data to Excel format"""
        try:
            output = BytesIO()
            
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                if 'sections' in data:
                    df = pd.DataFrame(data['sections'])
                    df.to_excel(writer, sheet_name=tab_name[:30], index=False)
                elif 'metadata' in data:
                    df = pd.DataFrame(data['metadata'])
                    df.to_excel(writer, sheet_name=tab_name[:30], index=False)
                elif 'reports' in data:
                    df = pd.DataFrame(data['reports'])
                    df.to_excel(writer, sheet_name=tab_name[:30], index=False)
                else:
                    # Generic handling
                    for key, value in data.items():
                        if isinstance(value, list):
                            df = pd.DataFrame(value)
                            df.to_excel(writer, sheet_name=key[:30], index=False)
            
            output.seek(0)
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Error creating Excel file: {e}")
            return None
    
    def download_file(self, category_url, event_target, file_info, save_path):
        """Download a file using ASP.NET two-step postback"""
        max_retries = 3
        
        for attempt in range(1, max_retries + 1):
            try:
                # STEP 1: Get the page to extract ViewState
                response = self.session.get(category_url, timeout=60)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Get ViewState data and all form fields
                form_data = self.get_viewstate_data(soup)
                form_data['__EVENTTARGET'] = event_target
                form_data['__EVENTARGUMENT'] = ''
                
                # Get the form and its action URL
                form = soup.find('form')
                form_action_url = category_url  # Default to page URL
                
                if form:
                    # Get form action if specified
                    form_action = form.get('action')
                    if form_action:
                        form_action_url = urljoin(category_url, form_action)
                    
                    # Get all form fields
                    all_inputs = form.find_all('input')
                    for inp in all_inputs:
                        name = inp.get('name')
                        if not name or name in form_data:
                            continue
                        
                        input_type = inp.get('type', '').lower()
                        
                        if input_type == 'checkbox' or input_type == 'radio':
                            if inp.get('checked'):
                                form_data[name] = inp.get('value', 'on')
                        else:
                            form_data[name] = inp.get('value', '')
                    
                    # Get all select/dropdown fields
                    all_selects = form.find_all('select')
                    for select in all_selects:
                        name = select.get('name')
                        if not name or name in form_data:
                            continue
                        
                        selected = select.find('option', selected=True)
                        if selected:
                            form_data[name] = selected.get('value', '')
                        else:
                            first_option = select.find('option')
                            form_data[name] = first_option.get('value', '') if first_option else ''
                    
                    # Get all textarea fields
                    all_textareas = form.find_all('textarea')
                    for textarea in all_textareas:
                        name = textarea.get('name')
                        if name and name not in form_data:
                            form_data[name] = textarea.get_text(strip=True)
                
                # Prepare headers for ASP.NET postback
                post_headers = {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Referer': category_url,
                    'Origin': 'https://www.csb.gov.kw',
                    'Accept': '*/*',
                    'Accept-Language': 'ar,en;q=0.9',
                    'Cache-Control': 'no-cache'
                }
                
                # STEP 2: Post to open detail/modal view
                logger.debug(f"Step 1: Posting to {event_target}")
                first_response = self.session.post(
                    form_action_url,
                    data=form_data,
                    headers=post_headers,
                    timeout=60
                )
                
                first_response.raise_for_status()
                
                # Check what we got
                content_type = first_response.headers.get('Content-Type', '')
                
                # If we got a file directly (some links might work in one step)
                if ('application/pdf' in content_type or 
                    'application/vnd' in content_type or 
                    'application/octet-stream' in content_type):
                    
                    content = first_response.content
                    if len(content) > 1000 or content[:4] == b'%PDF' or content[:2] == b'PK':
                        logger.debug("Got file in one step")
                        return content
                
                # If we got HTML, look for the download link
                if 'text/html' in content_type:
                    logger.debug("Got HTML, looking for download link...")
                    detail_soup = BeautifulSoup(first_response.content, 'html.parser')
                    
                    # Pattern 1: Look for the modal download link (lnk_down_file)
                    download_link = detail_soup.find('a', {'id': lambda x: x and 'lnk_down_file' in x})
                    
                    # Pattern 2: Look for RepeaterForChild links (expanded section pattern)
                    if not download_link:
                        logger.debug("lnk_down_file not found, checking RepeaterForChild...")
                        
                        # Determine which file type we want (PDF or Excel) based on original event target
                        want_pdf = 'LinkButton3' in event_target  # LinkButton3 = PDF
                        want_excel = 'LinkButton4' in event_target  # LinkButton4 = Excel
                        
                        # Find ALL RepeaterForChild links (expanded section may have multiple files)
                        repeater_links = detail_soup.find_all('a', {'id': lambda x: x and 'RepeaterForChild' in x})
                        matching_links = []
                        
                        for link in repeater_links:
                            img = link.find('img')
                            if img:
                                img_src = img.get('src', '').lower()
                                
                                # Collect ALL links matching file type
                                if want_pdf and 'pdf' in img_src:
                                    matching_links.append(link)
                                elif want_excel and ('xls' in img_src or 'excel' in img_src):
                                    matching_links.append(link)
                        
                        if matching_links:
                            logger.info(f"    Found {len(matching_links)} files in expanded section")
                            
                            # Download ALL files from expanded section
                            downloaded_count = 0
                            for idx, link in enumerate(matching_links, 1):
                                link_id = link.get('id', '')
                                
                                # Extract filename from table structure
                                # Link is in TD#2, filename is in TD#1 of same row
                                file_title = None
                                tr = link.find_parent('tr')
                                if tr:
                                    tds = tr.find_all('td')
                                    if len(tds) > 0:
                                        # First TD contains the filename
                                        file_title = tds[0].get_text(strip=True)
                                
                                # Fallback: Use index-based name
                                if not file_title:
                                    file_title = f"file_{idx}"
                                    logger.warning(f"      Could not extract filename from table, using fallback: {file_title}")
                                else:
                                    file_title = self.sanitize_filename(file_title)
                                
                                logger.info(f"    Downloading {idx}/{len(matching_links)}: {file_title[:50]}...")
                                
                                # Extract event target
                                href = link.get('href', '')
                                if '__doPostBack' in href:
                                    match = re.search(r"__doPostBack\('([^']+)'", href)
                                    if match:
                                        child_event_target = match.group(1)
                                        
                                        # Get fresh ViewState
                                        form_data2 = self.get_viewstate_data(detail_soup)
                                        form_data2['__EVENTTARGET'] = child_event_target
                                        form_data2['__EVENTARGUMENT'] = ''
                                        
                                        # Get all form fields
                                        form2 = detail_soup.find('form')
                                        if form2:
                                            for inp in form2.find_all('input'):
                                                name = inp.get('name')
                                                if name and name not in form_data2:
                                                    form_data2[name] = inp.get('value', '')
                                        
                                        # Download this file
                                        download_response = self.session.post(
                                            form_action_url,
                                            data=form_data2,
                                            headers=post_headers,
                                            timeout=60,
                                            stream=True
                                        )
                                        
                                        content_type = download_response.headers.get('Content-Type', '')
                                        
                                        if ('application/pdf' in content_type or 
                                            'application/vnd' in content_type or 
                                            'application/octet-stream' in content_type):
                                            
                                            content = download_response.content
                                            
                                            if len(content) > 1000 or content[:4] == b'%PDF' or content[:2] == b'PK':
                                                # Generate S3 path for this child file in a subfolder
                                                # Create subfolder with section name to organize child files
                                                parent_folder = save_path.rsplit('/', 1)[0]  # Get parent directory
                                                section_name = self.sanitize_filename(file_info['title'])
                                                extension = save_path.rsplit('.', 1)[-1]
                                                child_s3_path = f"{parent_folder}/{section_name}/{file_title}.{extension}"
                                                
                                                # Upload to S3
                                                if self.upload_to_s3(content, child_s3_path):
                                                    downloaded_count += 1
                                                    logger.info(f"      ✓ Uploaded: {file_title}.{extension}")
                                                else:
                                                    logger.error(f"      ✗ Failed to upload: {file_title}.{extension}")
                                        
                                        # Reload detail_soup for next iteration (ViewState may change)
                                        if idx < len(matching_links):
                                            time.sleep(0.5)  # Brief delay between downloads
                                            # Re-fetch the expanded section
                                            refresh_response = self.session.post(
                                                form_action_url,
                                                data=form_data,  # Use original form data to keep section expanded
                                                headers=post_headers,
                                                timeout=60
                                            )
                                            detail_soup = BeautifulSoup(refresh_response.content, 'html.parser')
                            
                            # Return a marker indicating expanded section was handled
                            if downloaded_count > 0:
                                return b'EXPANDED_SECTION_HANDLED'  # Special marker
                            else:
                                return None
                        
                        # If no matching links found, set download_link = None to continue
                        download_link = None
                    
                    if download_link:
                        logger.debug("Found download link, performing second postback...")
                        
                        # Extract event target from href
                        href = download_link.get('href', '')
                        if '__doPostBack' in href:
                            match = re.search(r"__doPostBack\('([^']+)'", href)
                            if match:
                                download_event_target = match.group(1)
                                
                                # STEP 3: Get fresh ViewState from detail view
                                form_data2 = self.get_viewstate_data(detail_soup)
                                form_data2['__EVENTTARGET'] = download_event_target
                                form_data2['__EVENTARGUMENT'] = ''
                                
                                # Get all form fields from detail view
                                form2 = detail_soup.find('form')
                                if form2:
                                    for inp in form2.find_all('input'):
                                        name = inp.get('name')
                                        if name and name not in form_data2:
                                            form_data2[name] = inp.get('value', '')
                                
                                # STEP 4: Post to download link
                                logger.debug(f"Step 2: Posting to {download_event_target}")
                                download_response = self.session.post(
                                    form_action_url,
                                    data=form_data2,
                                    headers=post_headers,
                                    timeout=60,
                                    stream=True
                                )
                                
                                download_response.raise_for_status()
                                
                                # Check if we got the file
                                content_type = download_response.headers.get('Content-Type', '')
                                
                                if ('application/pdf' in content_type or 
                                    'application/vnd' in content_type or 
                                    'application/octet-stream' in content_type or
                                    'application/x-download' in content_type):
                                    
                                    content = download_response.content
                                    
                                    # Verify it's actually a file
                                    if len(content) < 1000:
                                        try:
                                            if b'<html' in content.lower():
                                                logger.warning(f"Small file contains HTML, skipping")
                                                if attempt < max_retries:
                                                    time.sleep(3)
                                                    continue
                                                return None
                                        except:
                                            pass
                                    
                                    return content
                    
                    # If we couldn't find download link, log details
                    logger.warning(f"Could not find download link (lnk_down_file or RepeaterForChild) in response")
                    logger.debug(f"Response size: {len(first_response.content)} bytes")
                    
                    # Debug: count what we did find
                    all_postback_links = detail_soup.find_all('a', href=lambda x: x and '__doPostBack' in x)
                    repeater_links = [l for l in all_postback_links if 'RepeaterForChild' in l.get('id', '')]
                    logger.debug(f"Found {len(all_postback_links)} total postback links, {len(repeater_links)} RepeaterForChild links")
                
                # If we get here, something didn't work
                logger.warning(f"Unexpected content type: {content_type}")
                logger.warning(f"Event target was: {event_target}")
                
                # Retry if not last attempt
                if attempt < max_retries:
                    logger.info(f"  Retry {attempt}/{max_retries}...")
                    time.sleep(3)
                    continue
                
                logger.error(f"Failed after {max_retries} attempts. File: {file_info['title'][:50]}")
                return None
                    
            except Exception as e:
                logger.error(f"Error downloading file (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(3)
                    continue
                return None
        
        return None
    
    def file_exists_in_s3(self, s3_path):
        """Check if file already exists in S3 with caching"""
        # Check cache first
        if s3_path in self.s3_exists_cache:
            return self.s3_exists_cache[s3_path]
        
        try:
            with self.s3_lock:
                self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_path)
            self.s3_exists_cache[s3_path] = True
            return True
        except:
            self.s3_exists_cache[s3_path] = False
            return False
    
    def batch_check_s3_exists(self, s3_paths):
        """Batch check if multiple files exist in S3"""
        results = {}
        uncached_paths = [p for p in s3_paths if p not in self.s3_exists_cache]
        
        # Return cached results
        for path in s3_paths:
            if path in self.s3_exists_cache:
                results[path] = self.s3_exists_cache[path]
        
        # Check uncached paths
        for path in uncached_paths:
            exists = self.file_exists_in_s3(path)
            results[path] = exists
        
        return results
    
    def upload_to_s3(self, file_content, s3_path):
        """Upload file to S3 with thread safety"""
        try:
            with self.s3_lock:
                self.s3_client.put_object(
                    Bucket=self.bucket_name,
                    Key=s3_path,
                    Body=file_content
                )
            # Update cache
            self.s3_exists_cache[s3_path] = True
            logger.info(f"Uploaded to S3: {s3_path}")
            return True
        except NoCredentialsError:
            logger.error("AWS credentials not found")
            return False
        except Exception as e:
            logger.error(f"Error uploading to S3: {e}")
            return False
    
    def download_and_upload_file(self, args):
        """Thread-safe file download and upload wrapper"""
        category_url, event_target, file_info, s3_path, idx, total, tab_name, is_modal = args
        
        modal_prefix = "[Modal] " if is_modal else ""
        logger.info(f"    [{idx}/{total}] Downloading: {modal_prefix}{file_info['title'][:50]}...")
        
        try:
            # Download file
            file_content = self.download_file(category_url, event_target, file_info, s3_path)
            
            if file_content:
                # Check if this was an expanded section
                if file_content == b'EXPANDED_SECTION_HANDLED':
                    logger.info(f"    [{idx}/{total}] ✓ Expanded section files uploaded")
                    return {'status': 'success', 'type': 'expanded'}
                else:
                    # Upload to S3
                    if self.upload_to_s3(file_content, s3_path):
                        filename = s3_path.split('/')[-1]
                        logger.info(f"    [{idx}/{total}] ✓ Successfully uploaded: {filename}")
                        return {'status': 'success', 'type': 'single'}
                    else:
                        logger.error(f"    [{idx}/{total}] ✗ Failed to upload")
                        return {'status': 'failed', 'reason': 'upload_failed'}
            else:
                logger.error(f"    [{idx}/{total}] ✗ Failed to download")
                return {'status': 'failed', 'reason': 'download_failed'}
        except Exception as e:
            logger.error(f"    [{idx}/{total}] ✗ Error: {e}")
            return {'status': 'failed', 'reason': str(e)}
    
    def scrape_category(self, category_info):
        """Scrape all tabs and files for a category with parallel downloads"""
        main_category = self.sanitize_filename(category_info['main_category'])
        subcategory = self.sanitize_filename(category_info['subcategory'])
        category_url = category_info['url']
        
        logger.info(f"Processing: {main_category} -> {subcategory}")
        
        # Define the 4 tabs
        tabs = [
            {'name': 'الموضوع', 'id': 'T2'},
            {'name': 'النشرات الإحصائية', 'id': 'T3'},
            {'name': 'البيانات الوصفية', 'id': 'T4'},
            {'name': 'التقارير', 'id': 'T5'}
        ]
        
        stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}
        
        for tab in tabs:
            tab_name = self.sanitize_filename(tab['name'])
            logger.info(f"  Processing tab: {tab_name}")
            
            result = self.scrape_tab_content(category_url, tab['name'], tab['id'])
            files = result['files']
            text_content = result['text_content']
            
            # Prepare file tasks
            file_tasks = []
            for idx, file_info in enumerate(files, 1):
                stats['total'] += 1
                
                title = self.sanitize_filename(file_info['title'])
                file_type = file_info['file_type']
                event_target = file_info['event_target']
                is_modal = file_info.get('source') == 'modal'
                
                # Create S3 path
                extension = 'pdf' if file_type == 'pdf' else 'xlsx' if file_type == 'excel' else 'bin'
                filename = f"{title}_{idx}.{extension}"
                s3_path = f"{self.base_s3_path}/{main_category}/{subcategory}/{tab_name}/{filename}"
                
                file_tasks.append((s3_path, (category_url, event_target, file_info, s3_path, idx, len(files), tab_name, is_modal)))
            
            # Batch check S3 existence
            s3_paths = [task[0] for task in file_tasks]
            if s3_paths:
                existence_map = self.batch_check_s3_exists(s3_paths)
                
                # Filter out existing files
                download_tasks = []
                for s3_path, task_args in file_tasks:
                    if existence_map.get(s3_path, False):
                        idx, total = task_args[4], task_args[5]
                        title = task_args[2]['title']
                        is_modal = task_args[7]
                        modal_prefix = "[Modal] " if is_modal else ""
                        logger.info(f"    [{idx}/{total}] Skipping (already exists): {modal_prefix}{title[:50]}...")
                        stats['skipped'] += 1
                    else:
                        download_tasks.append(task_args)
                
                # Download files in parallel (2-3 concurrent downloads per tab)
                if download_tasks:
                    with ThreadPoolExecutor(max_workers=min(3, len(download_tasks))) as executor:
                        futures = [executor.submit(self.download_and_upload_file, task) for task in download_tasks]
                        
                        for future in as_completed(futures):
                            try:
                                result = future.result()
                                if result['status'] == 'success':
                                    stats['success'] += 1
                                else:
                                    stats['failed'] += 1
                            except Exception as e:
                                logger.error(f"    Task exception: {e}")
                                stats['failed'] += 1
                    
                    # Brief delay between tab processing to avoid overwhelming server
                    time.sleep(1)
            
            # Process text content if no files found
            if not files and text_content:
                stats['total'] += 1
                filename = f"{tab_name}_content.xlsx"
                s3_path = f"{self.base_s3_path}/{main_category}/{subcategory}/{tab_name}/{filename}"
                
                # Check if text content already exists
                if self.file_exists_in_s3(s3_path):
                    logger.info(f"    Skipping text content (already exists): {tab_name}")
                    stats['skipped'] += 1
                    continue
                
                logger.info(f"    Extracting text content from {tab_name}")
                
                # Convert text content to Excel
                excel_content = self.create_excel_from_data(text_content, tab_name)
                
                if excel_content:
                    if self.upload_to_s3(excel_content, s3_path):
                        stats['success'] += 1
                        logger.info(f"    Uploaded text content as Excel: {filename}")
                    else:
                        stats['failed'] += 1
                else:
                    stats['failed'] += 1
        
        return stats
    
    def run(self, filter_main_category=None):
        """Main execution method"""
        if filter_main_category:
            logger.info(f"Starting KCSB data scraping for category: {filter_main_category}")
        else:
            logger.info("Starting KCSB data scraping for ALL categories...")
        
        # Get all categories
        categories = self.get_categories()
        
        if not categories:
            logger.error("No categories found. Exiting.")
            return
        
        # Filter by main category if specified
        if filter_main_category:
            categories = [c for c in categories if c['main_category'] == filter_main_category]
            logger.info(f"Filtered to {len(categories)} subcategories in '{filter_main_category}'")
            
            if not categories:
                logger.error(f"No subcategories found for main category: {filter_main_category}")
                return
        
        # Statistics
        total_stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}
        stats_lock = Lock()
        
        # Process categories in parallel (2-3 at a time to avoid overwhelming server)
        def process_category_wrapper(idx, category):
            logger.info(f"\n[{idx}/{len(categories)}] Processing category...")
            stats = self.scrape_category(category)
            
            with stats_lock:
                total_stats['total'] += stats['total']
                total_stats['success'] += stats['success']
                total_stats['failed'] += stats['failed']
                total_stats['skipped'] += stats.get('skipped', 0)
            
            # Brief delay between categories to avoid overwhelming server
            time.sleep(1)
            return stats
        
        # Process categories with controlled parallelism (max 2 to avoid timeouts)
        with ThreadPoolExecutor(max_workers=min(2, len(categories))) as executor:
            futures = [executor.submit(process_category_wrapper, idx, cat) for idx, cat in enumerate(categories, 1)]
            
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Category processing error: {e}")
        
        # Final summary
        logger.info("\n" + "="*50)
        logger.info("SCRAPING COMPLETE")
        logger.info(f"Total files found: {total_stats['total']}")
        logger.info(f"New files uploaded: {total_stats['success']}")
        logger.info(f"Already existed (skipped): {total_stats['skipped']}")
        logger.info(f"Failed: {total_stats['failed']}")
        logger.info("="*50)


if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Scrape KCSB data and upload to S3')
    parser.add_argument(
        '--category',
        type=str,
        help='Filter by main category name (e.g., "الاحصاءات العامة")',
        default=None
    )
    parser.add_argument(
        '--workers',
        type=int,
        help='Number of parallel workers for downloading files (default: 5, recommended: 3-5)',
        default=5
    )
    parser.add_argument(
        '--no-parallel',
        action='store_true',
        help='Disable parallel processing (use sequential downloads)'
    )
    args = parser.parse_args()
    
    # Get AWS credentials from environment variables
    aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID')
    aws_secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    bucket_name = os.environ.get('AWS_BUCKET_NAME')
    
    if not all([aws_access_key, aws_secret_key, bucket_name]):
        logger.error("AWS credentials not found in environment variables")
        exit(1)
    
    # Adjust workers if no-parallel is set
    max_workers = 1 if args.no_parallel else args.workers
    
    # Warn if using high worker count
    if max_workers > 5 and not args.no_parallel:
        logger.warning(f"Using {max_workers} workers may cause timeouts. Recommended: 3-5")
    
    # Create scraper and run with optional category filter
    logger.info(f"Starting scraper with {max_workers} worker(s)")
    scraper = KCSBScraper(aws_access_key, aws_secret_key, bucket_name, max_workers=max_workers)
    scraper.run(filter_main_category=args.category)
