# GitHub Actions Workflow Guide

## Overview
The workflow has been updated to give you flexible control over what gets scraped.

## How to Use

### Option 1: Manual Trigger (Recommended)

1. **Go to GitHub Actions tab** in your repository
2. **Click "KCSB Data Scraper"** workflow
3. **Click "Run workflow"** button
4. **Choose your options**:

   **Category Selection:**
   - `all` - Scrape ALL categories (default)
   - `الاحصاءات العامة` - General Statistics only
   - `الاحصاءات السكانية` - Population Statistics only
   - `الإحصاءات الاقتصادية` - Economic Statistics only
   - `الإحصاءات التجارية والزراعية` - Trade & Agriculture only
   - `الاحصاءات الاجتماعية والخدمات` - Social Services only

   **Workers** (parallel downloads):
   - `1` - Sequential (slowest, most reliable)
   - `3` - Conservative (good for unstable connections)
   - `5` - Recommended (default - balanced)
   - `8` - Aggressive (fast connections only)

5. **Click "Run workflow"**

### Option 2: Scheduled Runs

The workflow runs **automatically quarterly**:
- January 1st at 12:00 PM UTC
- April 1st at 12:00 PM UTC
- July 1st at 12:00 PM UTC
- October 1st at 12:00 PM UTC

**Scheduled runs process ALL categories** with 5 workers by default.

## Examples

### Example 1: Test with One Category
**Use Case**: You want to test the scraper on just population statistics

**Settings**:
- Category: `الاحصاءات السكانية`
- Workers: `5`

**Expected Time**: ~5-10 minutes

### Example 2: Full Scrape with Conservative Settings
**Use Case**: Scrape everything but you have a slower connection

**Settings**:
- Category: `all`
- Workers: `3`

**Expected Time**: ~45-60 minutes

### Example 3: Quick Full Scrape
**Use Case**: Fast connection, scrape everything quickly

**Settings**:
- Category: `all`
- Workers: `5`

**Expected Time**: ~30-40 minutes

### Example 4: Debug Single Category
**Use Case**: One category is failing, debug it sequentially

**Settings**:
- Category: (select the failing one)
- Workers: `1`

**Expected Time**: ~10-15 minutes per category

## What Changed from Before

### Before:
- 5 separate jobs running in parallel
- No way to choose specific categories
- No worker control
- Complex workflow with duplicated code

### After:
- ✅ Single flexible job
- ✅ Choose specific category OR all
- ✅ Control worker count
- ✅ Cleaner, easier to maintain
- ✅ Scheduled runs still work
- ✅ Can test single categories quickly

## Monitoring Progress

### During Workflow Run:

1. Go to **Actions** tab
2. Click on your running workflow
3. Click on **"Scrape KCSB Data"** job
4. Expand steps to see real-time logs

### Look for:
```
INFO - Starting scraper with 5 worker(s)
INFO - Processing: الاحصاءات السكانية -> ...
INFO - [1/20] Downloading: ...
INFO - ✓ Successfully uploaded: ...
```

### Logs are saved as artifacts:
- Available for 7 days after run
- Download from workflow run page
- Name: `scraper-logs-<run-number>`

## Troubleshooting

### Workflow Fails with Timeouts:
**Solution**: Re-run with fewer workers (3 instead of 5)

### Want to Scrape Just One Category:
**Solution**: Use manual trigger and select specific category

### Need Sequential (No Parallelism):
**Solution**: Select workers = `1`

### Scheduled Run Behavior:
- Always processes ALL categories
- Uses 5 workers by default
- Cannot be customized (scheduled runs don't have inputs)

## Benefits

1. **Flexibility**: Choose what to scrape
2. **Testing**: Test single categories before full scrape
3. **Debugging**: Run with 1 worker for debugging
4. **Speed Control**: Adjust workers based on your needs
5. **Cost Savings**: Only scrape what you need
6. **Simpler**: One job instead of five separate ones

## Summary

**Quick scrape one category:**
```
Category: الاحصاءات السكانية
Workers: 5
```

**Full scrape (default):**
```
Category: all
Workers: 5
```

**Reliable full scrape:**
```
Category: all
Workers: 3
```

**Debug mode:**
```
Category: <specific one>
Workers: 1
```
