"""Generic DolphinScheduler Python node; create the DATE parameter biz_date."""

import json
import logging
import os
from dataclasses import asdict
from datetime import date
from pathlib import Path
from tempfile import gettempdir

from trajectory_classifier import run_s3_label_job


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    biz_date = date.fromisoformat("${biz_date}").isoformat()
    result = run_s3_label_job(
        input_uri=os.environ["CLASSIFIER_INPUT_ROOT"].rstrip("/") + f"/dt={biz_date}/",
        output_uri=os.environ["CLASSIFIER_OUTPUT_ROOT"].rstrip("/")
        + f"/dt={biz_date}/",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        classifier_api_url=os.environ["CLASSIFIER_API_URL"],
        classifier_model=os.environ.get("CLASSIFIER_MODEL", "deepseek-v4.1-flash"),
        workspace_parent=Path(gettempdir()),
        max_workers=4,
        rate_limit_state_path=Path(os.environ["CLASSIFIER_RATE_LIMIT_STATE"]),
    )
    print(json.dumps(asdict(result), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
