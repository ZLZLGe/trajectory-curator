# 场景、能力、学科分类

分类工具对已经标准化的轨迹添加三类标签。它与 `trajectory-curator` 的完整性筛选、样本选择、需求拆解、难度分级分别运行。

## 安装与输入

```bash
python -m pip install -e '.[classification]'
# 使用 S3 时：
python -m pip install -e '.[classification,s3]'
```

输入是 TrajFoundry v2/v3/v4 格式的**标准化目录**，包含 `manifest.json`、清单列出的轨迹 JSONL 和 `lineage.jsonl`。支持历史分片式与当前扁平文件布局。只给一个任意 JSONL 文件或 BaseTraj 分片不足以运行批处理；如果已有解析后的轨迹字典，可以直接使用 Python 分类核心。

批处理会核验清单、文件大小与 SHA256、完整轨迹契约、来源和派生计数。分类过程不执行原始数据标准化。必要的契约实现已包含在本包的 `_contract/` 中，无需安装或导入原 TrajFoundry。

## 本地目录

```bash
export CLASSIFIER_API_URL='https://model.example.com/v1/chat/completions'
read -rs -p 'Classifier API key: ' CLASSIFIER_API_KEY
export CLASSIFIER_API_KEY
mkdir -p /data/state

trajectory-classifier local \
  --input /data/normalized/day \
  --output /data/classified/day \
  --model deepseek-v4.1-flash \
  --rate-limit-state /data/state/classifier-budget.json
```

`--api-key-file /path/to/key.txt` 可替代 Key 环境变量，`--api-url` 可替代 URL 环境变量。Key 不放在命令参数值、输出清单或日志中。URL 需要包含完整 `/chat/completions` 路径；模型服务应支持 JSON 输出模式和本客户端的 `thinking` 参数。

本地输出的 manifest 使用 `file://` 输入目录 URI。输入输出目录必须分开，路径检查也会拒绝通过符号链接逃出数据目录。

## S3 分区

```bash
trajectory-classifier s3 \
  --input s3://example-bucket/normalized/dt=2026-01-01/ \
  --output s3://example-bucket/classified/dt=2026-01-01/ \
  --endpoint-url https://s3.example.com \
  --model deepseek-v4.1-flash \
  --rate-limit-state /data/state/classifier-budget.json
```

默认使用 boto3 的 AWS 凭据链，例如环境变量、共享 credentials 文件或实例身份。也可指定 `--credentials /path/to/s3.json`，JSON 字段为 `aws_access_key_id`、`aws_secret_access_key` 和可选的 `aws_session_token`。指定的文件必须归当前用户所有、权限为 `0400` 或 `0600`，不能是符号链接。示例不包含凭据值。

## 规则、限流与恢复

- 根或任意子节点 `total_tool_calls > 0` 才调用模型；全为 0 则输出 `skipped/no_toolcall`。
- 模型输入只包含根轨迹清洗后的用户请求；环境、系统说明、工具返回、模型回复和子代理消息均排除。
- 成功标签必须来自固定目录：90 个场景中选 1 个，10 个能力中选 1–2 个，25 个学科选项中选 1 个。
- 默认 4 个线程、90 RPM、450 万 TPM、180 秒超时、最多 20 次额外 HTTP 重试；所有重试和上下文缩减后的请求均计入预算。
- 同一服务配额下的多个任务应指定同一个 `--rate-limit-state`。父目录需预先创建；跨主机时要求共享文件系统支持 POSIX 文件锁和原子替换。未指定则只在当前客户端内限流。
- 成功分类进入 SQLite 缓存。默认路径为临时工作目录下的 `.trajectory-classifier-cache/`，可用 `--state` 指定持久文件。缓存应放在可靠的本地磁盘；多 Worker 共享的是配额 JSON，而非 SQLite 缓存。
- 同输入、同配置、同缓存文件重跑时复用成功标签；单条失败会重试。变更 Prompt、标签目录、模型或请求设置会隔离缓存。调低并发或配额不改变标签缓存身份。

批处理在所有模型调用和本地契约校验完成后才开始写出，逐个回读校验轨迹和 lineage，最后发布 manifest。持续 404、认证或其他配置错误会在上传前终止。输出发布过程中同名文件可能先于 manifest 更新；读取方应按摘要校验，避免两个任务同时写同一输出目录。

`validation_valid=true` 表示程序校验通过。单条模型失败可作为 `classification.status=failed` 发布，因此还需检查 `failed` 计数；程序校验不代表人工确认每个标签的语义。

完整[字段与错误说明](contract.md)、[Prompt 说明](../../src/trajectory_classifier/resources/prompt.md)、[Python 与调度示例](../../examples/classification/README.md)。

## Python API

```python
from trajectory_classifier import run_local_label_job

result = run_local_label_job(
    input_directory="/data/normalized/day",
    output_directory="/data/classified/day",
    classifier_api_url="https://model.example.com/v1/chat/completions",
    classifier_model="deepseek-v4.1-flash",
    # CLASSIFIER_API_KEY 由环境提供
    rate_limit_state_path="/data/state/classifier-budget.json",
)
print(result)
```

`run_s3_label_job` 提供对应的 S3 参数；两个函数都在 `trajectory_classifier.jobs` 中实现，也从包顶层公开。单条内存分类可使用 `ChatCompletionsClient`、`ScenarioTaxonomy`、`TrajectoryClassifier`，见 [Python 示例](../../examples/classification/classify_one.py)。
