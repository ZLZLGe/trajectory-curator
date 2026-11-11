# 分类 Prompt 与资源

分类器版本：`2026-11-09.2`。Prompt 版本：`v006-toolcall-scenario-capability-subject`。

运行时的系统 Prompt 由 `classifier.py` 中的 `TrajectoryClassifier._build_system_prompt()` 与 `taxonomy.json` 中的固定目录共同生成。该函数是可执行 Prompt 的唯一来源；此文件解释其约束，不提供第二份可能不同步的 Prompt 副本。

输出必须是 JSON 对象，只含 `scenario_labels`、`capability_labels`、`subject_labels`。场景恰好 1 个，能力 1–2 个且不重复，学科恰好 1 个；没有明确主要学科时选择 `通用（非学科）`。

标签名称逐字使用目录值，描述和典型任务用于判定，不能生成新标签或附加解释。目录来自原分类实现的已验证快照，包含 90 个场景、10 个能力、25 个学科选项；安装后运行 `trajectory-classifier taxonomy` 可查看名称。

用户侧 Prompt 包围 `USER_REQUESTS_BEGIN` / `USER_REQUESTS_END` 之间的清洗后根轨迹用户请求，明确将其当作分类证据。根轨迹的助手、工具、系统、developer 内容与子代理消息均不发送。超长输入按用户轮次头尾保留，并在模型报告上下文超限时有界缩减。

固定标签目录、系统 Prompt、分类政策和模型参数分别参与摘要，成功缓存还绑定输入清单与轨迹内容。模型凭据不参与这些摘要，也不写入输出。
