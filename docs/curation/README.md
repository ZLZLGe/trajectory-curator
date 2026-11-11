# trajectory-curator

从 Hugging Face 的 **BaseTraj Linux 轨迹**中获取一批有实际工具调用的数据：识别主代理与子代理、排除 compact 导致的历史残缺、统计真实用户轮数，再调用模型分析每条轨迹的独立需求、内部子任务和难度等级。

这个仓库把一次性的“找出 1,000–2,000 条合适轨迹”整理成可重复执行的工具。后续调整样本量或数据版本，可以复用同一套流程。产物包含原始轨迹、逐条标注、来源定位和校验报告；轨迹中的任务内容只作为待分析数据。

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
  --endpoint https://model.example.com/v1/chat/completions --model glm-5.3 --workers 16
trajectory-curator export --candidates /data/run/selected.jsonl --grades /data/run/grades \
  --plan /data/run/plan.json --cache /data/cache --count 1500 --output /data/delivery
trajectory-curator verify --output /data/delivery
```

`grade --limit 4` 可先试跑，随后去掉 `--limit` 继续。同一输出目录自动复用模型、prompt、量表、参数与用户文本均匹配的成功结果；复用时会按当前覆盖规则重新检查需求引用，漏标的旧缓存会被标为待复核，无需再次调用模型。配置改变时应使用新目录。默认最多每分钟 60 次请求（含重试），可用 `--requests-per-minute` 调整；并发数同时限制在途请求数量。网络错误与格式错误有有限次数重试，认证失败会停止批次。API 调用会消耗所配置服务的额度。

## 筛选口径

- **实际工具调用（硬条件）**：`total_tool_calls >= 1`，且主轨迹的 assistant 消息至少包含一次实际工具调用。只有工具定义、文本中提到工具、工具返回或子代理内部调用均不够；筛选、最终选样、导出和逐条校验都会检查这一条件。
- **主/子代理**：结合 `completeness.is_subagent`、父代理元数据、明确的子代理指令及委派封套。`sub_session_id` 或目录名本身不能证明子代理身份；主代理调用过子代理也不等于它自身是子代理。
- **compact**：根据明确的续接摘要、归档封套、compaction 元数据或记录项判断。元数据的正向证据为 `is_compaction=true`、`epoch_cause="compaction"`、`compaction=true` 或 `compaction.confirmed=true`；布尔值须为 JSON `true`。单独的 `compaction={"confirmed": false}` 不构成压缩证据，也不会覆盖另一个字段或续接摘要中的正向证据。根与子节点的元数据均检查。普通讨论 compact 不直接视为边界。对存在边界、不能证明历史完整的记录保守剔除。
- **真实用户轮数**：去掉工具结果、环境、AGENTS.md、IDE 文件上下文、技能展开正文、代理通知、续接摘要、建议下一句提示和 `/model` 等本地控制命令；`/goal` 中的实际任务仍保留。中断提示与真实请求合并时，只删除提示。一个原始 user 消息提取出多个内容片段，仍计一轮，同时另外保存片段数。这里是实际请求轮次，不保证每条请求都由人手工输入；主控程序派发的正式请求也可计入。
- **质量与完整性**：筛选阶段允许识别到 `missing_tool_result`、`no_final_assistant_turn` 的候选，但最终选样只取 `root_complete=true`、无 normalization reason code、工具定义完整、工具调用一致且无幻觉调用的记录。
- **去重与覆盖**：同一会话或完全相同的用户请求序列归为一组，优先保留内容较完整的版本。按 harness/长度分层、层内交错选择不同模型；这是多样性选样，不代表全库的统计分布。

判定结果是“未发现子代理或 compact 边界证据”，不等于人工证明所有历史都绝对完整。保留来源与审计字段便于后续复查。

compact 会把早期上下文替换成摘要。若采集到的轨迹只保留摘要和后续对话，原始需求、约束、撤销或修正可能已经丢失；例如后续只说“继续按刚才的要求实现”，无法从这句话恢复完整任务。筛选用于防止这类片段进入要求历史完整的交付集，影响真实用户轮数、需求数量和难度标注。

当前只输出 `no_observed_boundary`（未发现边界）或 `present_or_uncertain`（存在或不确定），不配对、拼接或恢复 compact 前后历史。由指令变化产生的 `epoch_cause="system_change"` 也不等于发生过 compact。

筛选版本已更新为 `screening-v6-confirmed-compaction`，再次执行 `screen` 会重算旧版本缓存。旧版本生成的交付需要重新 `screen`、`select`、`grade`、`export`，再按当前版本执行 `verify`；其中配置与用户文本相同的成功模型标注可复用。

## 输出字段

| 文件 | 内容 |
| --- | --- |
| `轨迹/<ID>.json` | 原始 JSONL 对应行，保持原字段与原始字节 |
| `真实用户轮次.jsonl` | 抽出的用户文本、原消息位置、真实轮数、片段数、身份和 compact 审计 |
| `难度分级.csv` | 便于查看的逐条汇总：需求数、子任务数、等级、分数、摘要、理由、汇总工具调用数和主轨迹实际调用数 |
| `难度分级.jsonl` | 完整需求标注、各需求难度、评分维度、需求覆盖审计、调用参数、用量及耗时 |
| `来源清单.jsonl` | 数据集 commit、分片、行号、source_key、原始文件 SHA256 |
| `manifest.json` | 样本统计、文件哈希、模型配置和参考版本 |
| `校验报告.json` | 每条轨迹、用户抽取、来源哈希与评分的复算结果 |

**独立需求数**是可单独验收的目标数量；**内部子任务数**是每个有效需求下的步骤/组成部分数量；`n_steps` 是量表估计的主要工作动作数，三者分别保存。确认、“继续”和返工通常服务于已有目标，不各自构成一个新需求。

难度用指定仓库的七维量表合成为 0–100 分，再映射为 1–5 级（分界 20/40/60/80）。CSV 的“难度等级/分数”取所有有效需求中的最高值，同时保留主需求难度。专业工时是需求难度的估计，不能当作助手实际耗时。分级属于模型标注，程序校验通过也不等于人工确认语义全对；`needs_review=true`、无有效需求或标注失败的记录不进入最终交付。

程序将全部用户轮次与各需求的 `source_turns` 对照，检查是否遗漏请求。只有明确列入规则的简短确认、选项回答、寒暄或进度询问可以免引用；“加测试”这样的短请求仍需覆盖。连续相同文本按发送给模型时的合并规则共享覆盖，非连续重复不会自动豁免；已取消需求的引用也算已交代。

`coverage_audit` 保存策略版本、必需／已覆盖／豁免／未覆盖的轮次 ID。未覆盖轮次会使 `model_output.needs_review=true`，并在 `review_reason` 追加具体 ID，保留模型原有复核理由。`status=success` 只表示取得结构有效的模型输出，仍须检查复核标记。`export` 和 `verify` 都独立重算覆盖，不依赖旧缓存是否带有审计字段。这个检查能发现未引用的轮次；引用已存在时，需求摘要是否准确完整仍需语义复核。

## 判定规则来源和测试

用户抽取、需求 prompt、量表与评分代码固定自 `damowangdongdong/traj2env` 的 **main** 分支 commit `bc1b28a216c1ed068860e8f4886440ff9b7c5163`。主/子代理及 compact 识别参考 `ZLZLGe/duanyanonlinedataprocessing-dev` commit `9a59b1ea044c355b42c7cf6c764e6830efa1065f`。逐文件来源及 SHA256 见 `src/trajectory_curator/resources/upstream.json`。

```bash
pytest -q
```

测试覆盖真实用户抽取、代理身份、compact 正反证据及旧缓存重筛、会话去重、模型格式纠错、需求漏标与缓存复查，以及原始轨迹导出与篡改检测。

已完成的实际批次见 [2026-11-07：1,500 条轨迹交付](2026-11-07-result.md)。
