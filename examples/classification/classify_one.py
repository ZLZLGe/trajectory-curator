"""Run one synthetic classification. Set URL and key through environment variables."""

import json
import os

from trajectory_classifier import (
    ChatCompletionsClient,
    ScenarioTaxonomy,
    TrajectoryClassifier,
)


def main():
    client = ChatCompletionsClient(
        api_url=os.environ["CLASSIFIER_API_URL"],
        api_key=os.environ["CLASSIFIER_API_KEY"],
        model=os.environ.get("CLASSIFIER_MODEL", "deepseek-v4.1-flash"),
        requests_per_minute=90,
        tokens_per_minute=4_500_000,
        rate_limit_state_path=os.environ.get("CLASSIFIER_RATE_LIMIT_STATE"),
    )
    classifier = TrajectoryClassifier(
        client=client,
        taxonomy=ScenarioTaxonomy.load(),
        # Synthetic in-memory example; batch jobs calculate the real manifest hash.
        input_manifest_sha256="0" * 64,
    )
    trajectory = {
        "total_tool_calls": 1,
        "messages": [
            {"role": "user", "content": "修复登录接口的异常，并运行单元测试。"}
        ],
    }
    attempt = classifier.classify(trajectory)
    print(json.dumps(attempt.classification, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
