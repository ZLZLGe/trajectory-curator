# trajectory-curator

从 Hugging Face 的 **BaseTraj Linux 轨迹**中获取一批可用于需求分析的数据：识别主代理与子代理、排除 compact 导致的历史残缺、统计真实用户轮数，再调用模型分析每条轨迹的独立需求、内部子任务和难度等级。

这个仓库把一次性的“找出 1,000–2,000 条合适轨迹”整理成可重复执行的工具。后续换日期、样本量或数据版本，可以复用同一套流程。产物包含原始轨迹、逐条标注、来源定位和校验报告；轨迹中的任务内容只作为待分析数据。

## 能做什么

| 阶段 | 作用 |
| --- | --- |
| 获取 | 固定数据集 commit，按 harness 和上下文长度选分片，下载并核验 SHA256 |
| 筛选 | 检查子代理身份、compact 边界、工具调用完整性，抽取真实用户内容 |
| 选样 | 保留完整主轨迹，按会话和完整用户文本去重，兼顾长度与模型覆盖 |
| 标注 | 只把真实用户文本发送给模型，拆分独立需求与内部子任务，按固定量表评分 |
| 交付 | 导出原始轨迹、CSV/JSONL 标注、来源清单，逐条复算并检查一致性 |

当前读取 BaseTraj 的标准化 JSONL 与 `metadata/shards.jsonl`，支持 `claude-code`、`codex` 两类 harness。其他 schema 需要新增适配器。

## 安装和凭据

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

访问私有数据集使用已有 Hugging Face 登录，也支持 `HF_TOKEN`、`HF_TOKEN_PATH`、`HF_HOME`。模型调用从环境变量 `CURATOR_API_KEY` 读取凭据：

```bash
read -rs -p 'Model API key: ' CURATOR_API_KEY
export CURATOR_API_KEY
```

密钥、原始数据、运行缓存与标注结果放在仓库外。Git 只保存代码、固定版本的 prompt/量表及使用说明。

## 一次完整运行

下面以先标注 1,750 条、最终交付 1,500 条为例。候选数量需要根据筛选通过率调整。

```bash
trajectory-curator inventory --output /data/run/plan.json --rows-per-bucket 2500
trajectory-curator download --plan /data/run/plan.json --cache /data/cache
trajectory-curator screen --plan /data/run/plan.json --cache /data/cache --output /data/run/screen
trajectory-curator select --candidates /data/run/screen/candidates.jsonl --count 1750 --output /data/run/selected.jsonl
trajectory-curator grade --candidates /data/run/selected.jsonl --output /data/run/grades \
  --endpoint https://token.pjlab.org.cn/v1/chat/completions --model glm-5.3 --workers 16
trajectory-curator export --candidates /data/run/selected.jsonl --grades /data/run/grades \
  --plan /data/run/plan.json --cache /data/cache --count 1500 --output /data/delivery
trajectory-curator verify --output /data/delivery
```

`grade --limit 4` 可先试跑，随后去掉 `--limit` 继续。同一输出目录自动复用模型、prompt、量表、参数与用户文本均匹配的成功结果；配置改变时应使用新目录。默认最多每分钟 60 次请求（含重试），可用 `--requests-per-minute` 调整；并发数同时限制在途请求数量。网络错误与格式错误有有限次数重试，认证失败会停止批次。API 调用会消耗所配置服务的额度。

## 筛选口径

- **主/子代理**：结合 `completeness.is_subagent`、父代理元数据、明确的子代理指令及委派封套。`sub_session_id` 或目录名本身不能证明子代理身份；主代理调用过子代理也不等于它自身是子代理。
- **compact**：根据明确的续接摘要、归档封套、compaction 元数据或记录项判断。普通讨论 compact、粘贴被分析的对话不直接视为边界。对存在边界、不能证明历史完整的记录保守剔除。
- **真实用户轮数**：去掉工具结果、环境、AGENTS.md、IDE 文件上下文、技能展开正文、代理通知、续接摘要、建议下一句提示和 `/model` 等本地控制命令；`/goal` 中的实际任务仍保留。中断提示与真实请求合并时，只删除提示。一个原始 user 消息提取出多个内容片段，仍计一轮，同时另外保存片段数。这里是实际请求轮次，不保证每条请求都由人手工输入；主控程序派发的正式请求也可计入。
- **质量与完整性**：筛选阶段允许识别到 `missing_tool_result`、`no_final_assistant_turn` 的候选，但最终选样只取 `root_complete=true`、无 normalization reason code、工具定义完整、工具调用一致且无幻觉调用的记录。
- **去重与覆盖**：同一会话或完全相同的用户请求序列归为一组，优先保留内容较完整的版本。按 harness/长度分层、层内交错选择不同模型；这是多样性选样，不代表全库的统计分布。

判定结果是“未发现子代理或 compact 边界证据”，不等于人工证明所有历史都绝对完整。保留来源与审计字段便于后续复查。

## 输出字段

| 文件 | 内容 |
| --- | --- |
| `轨迹/<ID>.json` | 原始 JSONL 对应行，保持原字段与原始字节 |
| `真实用户轮次.jsonl` | 抽出的用户文本、原消息位置、真实轮数、片段数、身份和 compact 审计 |
| `难度分级.csv` | 便于查看的逐条汇总：需求数、子任务数、等级、分数、摘要、理由与原始工具调用数 |
| `难度分级.jsonl` | 完整模型输出、各需求难度、评分维度、调用参数、用量及耗时 |
| `来源清单.jsonl` | 数据集 commit、分片、行号、source_key、原始文件 SHA256 |
| `manifest.json` | 样本统计、文件哈希、模型配置和参考版本 |
| `校验报告.json` | 每条轨迹、用户抽取、来源哈希与评分的复算结果 |

**独立需求数**是可单独验收的目标数量；**内部子任务数**是每个有效需求下的步骤/组成部分数量；`n_steps` 是量表估计的主要工作动作数，三者分别保存。确认、“继续”和返工通常服务于已有目标，不各自构成一个新需求。

难度用指定仓库的七维量表合成为 0–100 分，再映射为 1–5 级（分界 20/40/60/80）。CSV 的“难度等级/分数”取所有有效需求中的最高值，同时保留主需求难度。专业工时是需求难度的估计，不能当作助手实际耗时。分级属于模型标注，程序校验通过也不等于人工确认语义全对；`needs_review=true`、无有效需求或标注失败的记录不进入最终交付。

## 判定规则来源和测试

用户抽取、需求 prompt、量表与评分代码固定自 `damowangdongdong/traj2env` 的 **main** 分支 commit `bc1b28a216c1ed068860e8f4886440ff9b7c5163`。主/子代理及 compact 识别参考 `ZLZLGe/duanyanonlinedataprocessing-dev` commit `9a59b1ea044c355b42c7cf6c764e6830efa1065f`。逐文件来源及 SHA256 见 `src/trajectory_curator/resources/upstream.json`。

```bash
pytest -q
```

测试覆盖真实用户抽取、代理身份、compact 误判边界、会话去重、模型格式纠错，以及原始轨迹导出与篡改检测。
