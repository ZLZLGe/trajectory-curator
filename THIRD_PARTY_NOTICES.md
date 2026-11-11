# 参考与固定版本

`src/trajectory_curator/vendor/` 和需求判定资源文件固定自 `damowangdongdong/traj2env`，commit `bc1b28a216c1ed068860e8f4886440ff9b7c5163`。

这些文件保持上游原文；来源路径和内容哈希记录在 `resources/upstream.json`。本仓库新增筛选、下载、选样、模型调用及交付封装，协议判定还参考 `ZLZLGe/duanyanonlinedataprocessing-dev` 的指定版本。

`src/trajectory_classifier/` 的分类核心及 `_contract/` 兼容层抽取自 `ZLZLGe/trajfoundry` 的已验证工作树，分类器版本为 `2026-11-09.2`，Prompt 为 `v006-toolcall-scenario-capability-subject`。逐文件来源、原始 SHA256 与基线 commit 保存在 `src/trajectory_classifier/resources/upstream.json`；其中包含基线 commit 之后的限流等工作树改动，不能仅用 commit 号替代该快照。

分类包在本仓库内调整命名空间、打包、通用配置及本地目录适配；保留原分类政策、标签目录、Prompt 和校验算法。原 TrajFoundry 仓库及其生产部署不属于本仓库的运行依赖。

`src/trajectory_os/` 抽取自本地 `/data/basetraj-upload/os-v1` 的最终 OS 分类实现。`runtime_os.py` 的直接证据判定、`session_inference.py` 的会话范围和补全规则保持原行为；`refine_sessions.py` 中的证据汇总提取为 `build_session_map`，直接读取原始记录并核对 `metadata.session_id`。来源文件和 SHA256 记录在 `src/trajectory_os/resources/upstream.json`，原有 18 项回归测试一并迁入。该包只使用 Python 标准库；来源目录是溯源信息，不是运行依赖。

本仓库只保存代码、固定资源、合成测试和使用说明；数据集内容、凭据、生产配置及运行结果另行存储。上述来源记录用于说明各部分来历，不改变上游代码或数据的授权条件。
