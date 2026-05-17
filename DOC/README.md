# KCSB Data Scraper

This project automatically scrapes statistical data from the Kuwait Central Statistical Bureau (KCSB) website and uploads it to AWS S3.

## Overview

The scraper extracts all PDFs and Excel files from the KCSB statistics portal ([https://www.csb.gov.kw](https://www.csb.gov.kw)) and organizes them in S3 with the same hierarchical structure as the website:

```
KCSB-data/
├── الاحصاءات العامة/
│   ├── المجموعة الإحصائية السنوية/
│   │   ├── الموضوع/
│   │   ├── النشرات الإحصائية/
│   │   ├── البيانات الوصفية/
│   │   └── التقارير/
│   └── ...
├── الاحصاءات السكانية/
├── الإحصاءات الاقتصادية/
├── الإحصاءات التجارية والزراعية/
└── الاحصاءات الاجتماعية والخدمات/
```

## Features

- ✅ Automatically scrapes all 5 main categories
- ✅ **Parallel processing** - Each category runs as a separate job to avoid timeouts
- ✅ **Incremental scraping** - Only downloads new files, skips existing ones in S3
- ✅ **Quarterly schedule** - Runs every 3 months (Jan, Apr, Jul, Oct)
- ✅ **Modal popup detection** - Finds files hidden in modal dialogs
- ✅ **Smart link validation** - Distinguishes between modal triggers and actual file downloads
- ✅ Processes all subcategories
- ✅ Downloads content from all 4 tabs (الموضوع، النشرات الإحصائية، البيانات الوصفية، التقارير)
- ✅ Downloads PDFs and Excel files
- ✅ Extracts text content from tabs and saves as Excel when no files available
- ✅ Handles ASP.NET postback mechanism for downloads
- ✅ Handles legacy SSL/TLS for government websites
- ✅ Uploads directly to AWS S3
- ✅ Maintains website structure in S3
- ✅ Automated via GitHub Actions
- ✅ Manual trigger support

## Setup

### 1. GitHub Repository Setup

1. Push this code to your GitHub repository
2. Go to **Settings** → **Secrets and variables** → **Actions**
3. Add the following secrets:

   - `AWS_ACCESS_KEY_ID`: Your AWS access key
   - `AWS_SECRET_ACCESS_KEY`: Your AWS secret key
   - `AWS_BUCKET_NAME`: Your S3 bucket name (e.g., `my-kcsb-data-bucket`)

### 2. AWS S3 Setup

1. Create an S3 bucket in your AWS account
2. Ensure your bucket has appropriate permissions
3. Create an IAM user with S3 write permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:PutObjectAcl",
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::YOUR-BUCKET-NAME/*",
        "arn:aws:s3:::YOUR-BUCKET-NAME"
      ]
    }
  ]
}
```

### 3. GitHub Actions Configuration

The workflow uses **parallel processing** to avoid GitHub Actions timeout limits:
- **5 parallel jobs** - one for each main category
- Each job processes only its assigned category
- All jobs run simultaneously, reducing total runtime from 6+ hours to ~1-2 hours
- **Quarterly schedule**: Runs automatically on the 1st of January, April, July, and October at 2 AM UTC
- **Incremental scraping**: Only downloads new files that don't exist in S3
- Allows manual execution via "Run workflow" button

To manually trigger the workflow:
1. Go to **Actions** tab in your repository
2. Select **KCSB Data Scraper** workflow
3. Click **Run workflow**
4. All 5 category jobs will start in parallel
## Local Development

### Prerequisites

- Python 3.11 or higher
- AWS credentials configured

### Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd Project7-CB

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
.\venv\Scripts\Activate.ps1 # Windows

# Install dependencies
pip install -r requirements.txt
```

### Running Locally

Set environment variables:

```bash
# Windows PowerShell
$env:AWS_ACCESS_KEY_ID="your-access-key"
$env:AWS_SECRET_ACCESS_KEY="your-secret-key"
$env:AWS_BUCKET_NAME="your-bucket-name"

# Linux/Mac
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_ACCESS_KEY="your-secret-key"
export AWS_BUCKET_NAME="your-bucket-name"
```

Run the scraper:

```bash
# Run all categories
python scraper.py

# Run a specific category only (for parallel processing)
python scraper.py --category "الاحصاءات العامة"
python scraper.py --category "الاحصاءات السكانية"
python scraper.py --category "الإحصاءات الاقتصادية"
python scraper.py --category "الإحصاءات التجارية والزراعية"
python scraper.py --category "الاحصاءات الاجتماعية والخدمات"
```

## Project Structure

```
Project7-CB/
├── .github/
│   └── workflows/
│       └── scrape-kcsb.yml    # GitHub Actions workflow
├── scraper.py                  # Main scraper script
├── requirements.txt            # Python dependencies
└── DOC/
    └── README.md               # Documentation
```

## How It Works

1. **Category Discovery**: The scraper first visits the main statistics page and extracts all categories and subcategories
2. **S3 Check**: Before downloading, checks if file already exists in S3
3. **Tab Processing**: For each subcategory, it processes 4 tabs containing different types of content
4. **Modal Detection**: Also scans for popup modals that contain additional files
5. **File Download**: Files are downloaded using ASP.NET postback mechanism (only if not in S3)
6. **S3 Upload**: Each new file is uploaded to S3 with a path that mirrors the website structure
7. **Logging**: Comprehensive logging tracks progress, skipped files, modal files, and any errors

## Parallel Processing

To avoid GitHub Actions timeout limits (6 hours), the workflow splits the work across **5 parallel jobs**:

### Sequential vs Parallel Execution

**Before (Sequential)**:
```
Total time: 6+ hours ❌ Exceeds GitHub Actions limit
Category 1 → Category 2 → Category 3 → Category 4 → Category 5
```

**After (Parallel)**:
```
Total time: ~1-2 hours ✅ Within limits
Category 1 ──┐
Category 2 ──┤
Category 3 ──┼── All run simultaneously
Category 4 ──┤
Category 5 ──┘
```

### How It Works

1. The workflow creates 5 independent jobs
2. Each job processes only one main category:
   - Job 1: الاحصاءات العامة
   - Job 2: الاحصاءات السكانية
   - Job 3: الإحصاءات الاقتصادية
   - Job 4: الإحصاءات التجارية والزراعية
   - Job 5: الاحصاءات الاجتماعية والخدمات
3. All jobs run at the same time on separate runners
4. Each job uploads to the same S3 bucket without conflicts
5. If one job fails, others continue independently

## Incremental Scraping

The scraper is designed to avoid re-downloading files that already exist in S3, making quarterly runs efficient.

### How It Works

**Before Download**:
```
For each file found on website:
  1. Generate S3 path (e.g., KCSB-Data/category/subcategory/file.pdf)
  2. Check if file exists in S3
  3. If EXISTS → Skip (log as "already exists")
  4. If NOT EXISTS → Download and upload
```

**Benefits**:
- **First run**: Downloads everything (full scrape)
- **Subsequent runs**: Only downloads new files added since last run
- **Time savings**: Quarterly runs complete much faster (only new content)
- **Cost savings**: Reduces bandwidth and processing costs
- **Data preservation**: Existing files remain untouched

### Example Output

```
2026-04-01 Processing: الاحصاءات العامة -> المجموعة الإحصائية السنوية
  Processing tab: النشرات الإحصائية
    Skipping (already exists): المجموعة الإحصائية السنوية 2023-2024
    Skipping (already exists): المجموعة الإحصائية السنوية 2021-2022
    Downloading: المجموعة الإحصائية السنوية 2024-2025  ← New file!
    Uploaded to S3: KCSB-Data/.../المجموعة الإحصائية السنوية 2024-2025.pdf

SCRAPING COMPLETE
Total files found: 150
New files uploaded: 5
Already existed (skipped): 145
Failed: 0
```

## Technical Details

### ASP.NET Handling
- Properly extracts and submits ViewState data for form submissions
- Captures all hidden form fields required for postback

### Modal Popup Detection
The KCSB website uses modal dialogs to display additional files. The scraper:
- Scans for `Panel_Statistic` popup modals on each page
- Extracts files from modal tables
- Validates link types to distinguish:
  - **Modal triggers**: Parent rows that open popups (skipped)
  - **Actual downloads**: Links with `__doPostBack` and file icons (processed)
- Files from modals are prefixed with `[Modal]` in logs

### Smart Link Validation
To avoid failed downloads, each link is validated before attempting download:
- ✅ Must have file icon (PDF or Excel)
- ✅ Must use `__doPostBack` JavaScript event
- ❌ Skip links with `data-toggle='modal'` (these open popups)
- ❌ Skip links with `onclick` containing 'modal'

This prevents the scraper from attempting to download HTML pages when it encounters navigation links.

### Other Features
- **Retry Mechanism**: Attempts up to 3 retries per file with exponential backoff
- **Content Validation**: Verifies downloaded content is actually a file (not HTML error page)
- **Respectful Scraping**: Includes delays between requests to avoid overloading the server
- **Error Handling**: Robust error handling with detailed logging
- **File Type Detection**: Automatically detects PDF and Excel files
- **Legacy SSL Support**: Custom SSL adapter for government websites using legacy TLS

## Monitoring

After each run, you can:
- Check the **Actions** tab for workflow execution logs
- Download artifacts (logs) from completed workflow runs
- View S3 bucket to see uploaded files

## Troubleshooting

### Common Issues

**Issue**: "AWS credentials not found"
- **Solution**: Verify that GitHub secrets are properly set

**Issue**: "No categories found"
- **Solution**: The website structure may have changed. Check the scraper logic.

**Issue**: Files not uploading to S3
- **Solution**: Verify S3 bucket name and IAM permissions

## Notes

- The scraper respects the website by adding delays between requests
- Large PDF files may take time to download
- **Quarterly schedule**: Runs automatically every 3 months (January 1, April 1, July 1, October 1)
- **First run**: Downloads all available data (may take 1-2 hours with parallel processing)
- **Subsequent runs**: Only downloads new files added since last run (typically completes in minutes)
- **Incremental approach**: Existing files in S3 are never re-downloaded or overwritten
- You can manually trigger the workflow anytime to check for new content

## License

This project is for educational and archival purposes only. Please respect the KCSB website's terms of service and usage policies.

## Support

For issues or questions, please open a GitHub issue in this repository.