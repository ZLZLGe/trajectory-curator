# 分类示例

安装 `.[classification]`，使用 S3 时安装 `.[classification,s3]`。

- [classify_one.py](classify_one.py)：对内存中的合成轨迹生成场景、能力、学科标签。
- [dolphinscheduler_node.py](dolphinscheduler_node.py)：按业务日期处理 S3 分区的通用 Python 节点。
- 本地目录与 S3 命令见[分类文档](../../docs/classification/README.md)。两种批处理都要求输入目录直接含标准化 manifest；任意原始 JSONL 需先标准化。

示例通过环境变量读取 URL、Key、模型、存储位置和共享配额文件。为同一模型服务账号运行的任务配置同一个配额状态路径，每个输出分区只运行一个写入任务。

调度节点所需变量：

| 变量 | 用途 |
| --- | --- |
| `CLASSIFIER_API_URL` | 完整的 Chat Completions URL |
| `CLASSIFIER_API_KEY` | 模型凭据 |
| `CLASSIFIER_MODEL` | 模型名，默认 `deepseek-v4.1-flash` |
| `CLASSIFIER_RATE_LIMIT_STATE` | 已创建父目录下的共享预算 JSON 路径 |
| `CLASSIFIER_INPUT_ROOT` | 标准化 S3 根目录，例如 `s3://example-bucket/normalized/` |
| `CLASSIFIER_OUTPUT_ROOT` | 分类输出根目录，例如 `s3://example-bucket/classified/` |
| `S3_ENDPOINT_URL` | S3 服务地址 |
| `S3_REGION` | 区域，默认 `us-east-1` |

S3 凭据由标准 AWS 凭据链提供。DolphinScheduler 创建 DATE 参数 `biz_date`，值为 `$[yyyy-MM-dd]`；环境配置使用安装了本仓库的 Python。推荐按日期串行补数，避免多个任务重复写同一分区。
