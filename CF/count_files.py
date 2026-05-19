import os
import argparse
import boto3
from botocore.config import Config

def count_files(prefix: str) -> int:
    """Return the number of objects under `prefix` in the R2 bucket."""
    r2 = boto3.client(
        's3',
        endpoint_url=os.environ['CF_R2_ENDPOINT_URL'],
        aws_access_key_id=os.environ['CF_R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['CF_R2_SECRET_ACCESS_KEY'],
        region_name='us-east-1',          # dummy — R2 ignores region
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'}
        )
    )

    bucket = os.environ['CF_R2_BUCKET_NAME']

    # Normalise: strip leading slash, ensure trailing slash so we don't
    # accidentally match a sibling with a longer name prefix.
    prefix = prefix.lstrip('/')
    if prefix and not prefix.endswith('/'):
        prefix += '/'

    paginator = r2.get_paginator('list_objects_v2')
    total = 0
    page_num = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        page_num += 1
        count = len(page.get('Contents', []))
        print(f"  Page {page_num}: {count} objects (IsTruncated={page.get('IsTruncated', False)})")
        total += count

    print(f"  Total pages fetched: {page_num}")
    return total


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Count objects in a Cloudflare R2 folder (prefix).'
    )
    parser.add_argument(
        'path',
        nargs='?',
        default='',
        help='R2 prefix / folder path, e.g. "KCSB-Data/الاحصاءات العامة/". '
             'Leave empty to count the entire bucket.'
    )
    args = parser.parse_args()

    required = ['CF_R2_ACCESS_KEY_ID', 'CF_R2_SECRET_ACCESS_KEY',
                'CF_R2_ENDPOINT_URL', 'CF_R2_BUCKET_NAME']
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"ERROR: missing environment variables: {', '.join(missing)}")
        raise SystemExit(1)

    bucket = os.environ['CF_R2_BUCKET_NAME']
    display_path = args.path or '(entire bucket)'
    # Show the normalised prefix so you can verify it matches your R2 keys
    normalised = args.path.lstrip('/')
    if normalised and not normalised.endswith('/'):
        normalised += '/'
    print(f"Input path         : {display_path}")
    print(f"Normalised prefix  : '{normalised}' (empty = entire bucket)")
    print(f"Bucket             : {bucket}")

    n = count_files(args.path)
    print(f"Total files        : {n}")
