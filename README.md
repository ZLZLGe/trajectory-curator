# trajectory-curator

三套独立的轨迹处理工具，分别用于**轨迹整理与难度标注**、**场景／能力／学科分类**、**运行操作系统分类**。可以各自运行，也可以按自己的数据流程组合使用。

| 工具 | 输入与用途 | 入口 | 文档 |
| --- | --- | --- | --- |
| 轨迹整理 | 获取固定版本的 BaseTraj 数据，筛选完整且有主轨迹实际工具调用的样本，提取真实用户请求，标注需求与难度，导出并校验 | `trajectory-curator` | [整理流程](docs/curation/README.md) |
| 轨迹分类 | 读取已经标准化的轨迹，按用户请求选择场景、能力、学科标签，支持本地目录与 S3 批处理 | `trajectory-classifier` | [分类流程](docs/classification/README.md) |
| OS 分类 | 根据主代理的实际运行证据识别 Windows、Linux、macOS；支持同会话证据补全，证据不足时保留 `unknown` | `trajectory_os` Python API | [OS 分类](docs/os/README.md) |

整理工具的难度分级与分类工具的三类标签分别保留自己的 Prompt、规则、输出和缓存。OS 分类使用确定性规则，无需模型调用；三套工具可以独立使用。

## 安装

需要 **Python 3.11+**。场景／能力／学科分类的共享限流实现使用 POSIX 文件锁，支持 Linux/macOS。

```bash
git clone https://github.com/ZLZLGe/trajectory-curator.git
cd trajectory-curator
python -m venv .venv
source .venv/bin/activate

# 使用轨迹整理或 OS 分类
python -m pip install -e .

# 同时使用本地分类；S3 支持再添加 s3 extra
python -m pip install -e '.[classification,s3]'
```

此仓库包含独立的 `trajectory_curator`、`trajectory_classifier` 与 `trajectory_os` Python 包，无需安装 TrajFoundry。分类包中的 `_contract/` 保存读取标准化数据所需的契约与校验快照；OS 分类核心只使用 Python 标准库。

## 使用入口

轨迹整理：

```bash
trajectory-curator --help
trajectory-curator inventory --output /data/run/plan.json --rows-per-bucket 2500
```

完整步骤为获取、筛选、选样、难度标注、导出和验证，见[整理文档](docs/curation/README.md)和[示例](examples/curation/README.md)。

轨迹分类：

```bash
trajectory-classifier --help
trajectory-classifier taxonomy

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

输入目录须直接包含标准化 `manifest.json` 及其列出的文件。分类生成 **1 个场景、1–2 个能力、1 个主要学科**；根或子节点均无工具调用的轨迹原样保留并标记 `skipped`。详见[分类规则与字段](docs/classification/contract.md)、[本地/S3 示例](examples/classification/README.md)。

模型凭据从环境变量或外部文件读取，S3 支持标准 AWS 凭据链和指定的安全 JSON 文件。数据、模型输出、凭据与运行缓存放在仓库外。

运行操作系统分类：

```python
from trajectory_os import classify

annotation = classify({"metadata": {"platform": "darwin"}})
print(annotation["runtime_os"])  # mac
```

`classify` 返回分类、判定方法和证据，保持输入字典不变。需要同会话补全时，使用 `build_session_map` 和 `refine`，见 [OS 分类规则与 API](docs/os/README.md)和[合成示例](examples/os/classify_one.py)。

## 文件结构

```text
src/
  trajectory_curator/         # 下载、筛选、需求拆解、难度标注、交付
    resources/               # 需求 Prompt、难度量表、固定来源
    vendor/                  # 整理流程使用的上游代码
  trajectory_classifier/     # 场景、能力、学科分类
    resources/               # 标签目录、Prompt 说明、来源快照
    _contract/               # 标准化数据契约、身份计算与校验
  trajectory_os/             # 运行 OS 判定、同会话证据汇总与补全
    resources/               # 本地来源快照
docs/{curation,classification,os}/
examples/{curation,classification,os}/
tests/{curation,classification,os}/
```

## 开发与验证

```bash
python -m pip install -e '.[classification,s3,dev]'
python -m pytest -q
python -m build
```

测试覆盖原整理流程、分类规则、模型输入隔离、网络与格式重试、共享 RPM/TPM 限流、缓存恢复、本地/S3 批处理、发布失败边界，以及 OS 证据优先级、远程环境排除和同会话补全边界。测试使用合成轨迹和模拟接口，不调用模型服务，也不访问实际数据集。

代码与资源来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。已完成的整理批次见[1,500 条轨迹交付记录](docs/curation/2026-11-07-result.md)。
