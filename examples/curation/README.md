# 轨迹整理示例

此入口完成 BaseTraj 下载、完整性与工具调用筛选、选样、需求和难度标注、导出及校验。输出是原始轨迹与独立标注侧车。

```bash
trajectory-curator inventory --output /data/run/plan.json --rows-per-bucket 2500
trajectory-curator download --plan /data/run/plan.json --cache /data/cache
trajectory-curator screen --plan /data/run/plan.json --cache /data/cache --output /data/run/screen
trajectory-curator select --candidates /data/run/screen/candidates.jsonl \
  --count 1750 --output /data/run/selected.jsonl

read -rs -p 'Curator API key: ' CURATOR_API_KEY
export CURATOR_API_KEY
trajectory-curator grade --candidates /data/run/selected.jsonl --output /data/run/grades \
  --endpoint https://model.example.com/v1/chat/completions --model glm-5.3 --workers 16

trajectory-curator export --candidates /data/run/selected.jsonl --grades /data/run/grades \
  --plan /data/run/plan.json --cache /data/cache --count 1500 --output /data/delivery
trajectory-curator verify --output /data/delivery
```

私有 Hugging Face 数据集使用已有登录或 `HF_TOKEN`。`CURATOR_API_KEY` 仅供需求与难度标注；分类命令使用自己的 `CLASSIFIER_API_KEY`。详细规则见[整理文档](../../docs/curation/README.md)。
