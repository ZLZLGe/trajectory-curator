# 运行操作系统分类

`trajectory_os` 从轨迹中的运行证据识别主代理的操作系统，输出 `windows`、`linux`、`mac` 或 `unknown`。核心代码来自本地 BaseTraj OS 分类实现，以独立 Python API 提供，只依赖标准库。

## 单条分类

```bash
python -m pip install -e .
```

```python
from trajectory_os import classify

record = {
    "messages": [{"role": "system", "content": "Platform: darwin"}],
}
annotation = classify(record)
# {
#     "runtime_os": "mac",
#     "runtime_os_method": "explicit",
#     "runtime_os_evidence": [
#         {"os": "mac", "source": "messages/0/Platform", "value": "darwin"}
#     ],
# }
```

输入为已解析的轨迹字典。判定读取根轨迹的 `metadata`、`environment` / `environment_context` / `runtime` / `host`、`instructions` 和 `messages`；消息内容支持字符串和文本块，工具调用支持 `tool_calls` 与 `tool_use`。常规单条分类不要求 `_basetraj`；Codex 原生 memory job 的 `rollout_context` 特例需要 `_basetraj.harness == "codex"`。

函数返回新的标注字典，原始轨迹保持不变。若输出为 BaseTraj 记录，可由调用方将标注写入 `_basetraj`，保留其原有来源信息。

## 判定规则

按下表顺序选取第一个存在证据的层级；该层证据冲突或明确指向不支持的系统时，结果为 `unknown`。

| 方法 | 证据 |
| --- | --- |
| `explicit` | 运行元数据中的平台字段，或系统／环境声明中的明确 OS |
| `environment_path` | 运行工作目录、workspace roots；特定部署目录与明确 POSIX shell 的组合 |
| `local_tool` | 实际本地 shell 调用的工作目录、`cd`，以及可关联的简单 `uname` / `pwd` 等查询结果 |
| `session_context` | 可选的同会话直接证据补全，需调用下文 API |
| `unknown` | 缺少证据、不支持的 OS，或同层证据冲突 |

Windows 盘符和 UNC 路径、macOS 的 `/Users` 等目录、Linux 的 `/home`、`/root` 等目录参与路径推断。`/data`、`/workspace` 等部署目录需同时存在明确 POSIX shell。路径规则属于推断；单独的 shell 名称或通用 `/tmp` 不足以确定 OS。

分类对象是主代理的实际运行环境。macOS 上的代理通过 SSH 操作 Linux 时仍归为 `mac`；明确在 WSL 或 Linux 容器中运行的代理归为 `linux`。远程命令返回、子代理环境、普通对话中的系统名称及引用代码不作为本地主代理证据。Codex memory job 只读取顶层 `rollout_context` 的本地路径字段。

## 同会话补全

`build_session_map(records)` 接受包含完整会话成员的可迭代对象，重新计算每条记录的直接证据，再构造补全映射。输入记录需要以下来源字段：

```python
{
    "metadata": {"session_id": "12345678-1234-5678-1234-123456789abc"},
    "_basetraj": {
        "harness": "codex",
        "source_key": "classified/dt=2026-07-01/12345678-1234-5678-1234-123456789abc_sub_0.jsonl",
    },
    "messages": [...],
}
```

匹配范围为 **相同 harness、相同日期、相同 UUID session**。文件名需匹配 `/dt=<date>/<UUID>_sub_<number>.jsonl`，原始 `metadata.session_id` 也必须一致。范围内所有直接分类结果必须一致；身份不匹配、证据冲突或不支持的 OS 会阻止该会话补全。没有可补全记录的会话不写入映射。

```python
from trajectory_os import build_session_map, classify, refine

# records 是包含完整会话成员的轨迹列表。
session_map = build_session_map(records)
annotations = [refine(record, classify(record), session_map) for record in records]
```

`refine` 只补全 `runtime_os_reason == "no_runtime_evidence"` 的记录，保留已确定结果和冲突结果。补全证据指向一条具有直接证据的原始记录；已有 `session_context` 标注不会作为新的证据传播。构建映射时应包含范围内所有成员，以便发现会话中可能存在的冲突；若传入生成器，补全时需重新遍历输入。

完整合成示例：

```bash
python examples/os/classify_one.py
```

## 输出字段

| 字段 | 内容 |
| --- | --- |
| `runtime_os` | `windows`、`linux`、`mac`、`unknown` |
| `runtime_os_method` | 判定方法，见上表 |
| `runtime_os_evidence` | 含 `os`、`source`、`value` 的证据列表 |
| `runtime_os_reason` | 仅未知结果携带：`no_runtime_evidence`、`unsupported_os` 或 `conflicting_<method>` |

`OS_VALUES` 和 `NEW_FIELDS` 分别导出支持的类别和标注字段名。会话映射中的 `source_key`、`runtime_os`、`method`、`supporting_records` 保留原实现的结构。

## 来源与验证

来源文件及原始 SHA256 见 [`resources/upstream.json`](../../src/trajectory_os/resources/upstream.json)。直接分类与 `refine` 规则保持原行为；会话汇总从原本的数据重打包脚本中提取，直接读取输入记录的来源与会话元数据。

```bash
python -m pytest -q tests/os
```

原有 18 项回归测试覆盖平台声明、路径、WSL、远程调用、引用文本、冲突和会话范围；新增测试覆盖会话证据汇总、身份核对、补全边界及输入不变性。
