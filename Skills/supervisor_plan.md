---
name: supervisor_plan
description: 根据用户需求、executor 当前实际可用的工具和 skill 简介，生成按依赖顺序排列、可执行且可验收的任务计划。
---

# Supervisor Plan

根据用户问题、`executor_tools` 和 skill 简介制定最短可行计划。计划的目的不是把分析过程拆得越细越好，
而是以尽量少的 task 和模型回合获得足够证据并交付结果。

## Executor 工具名称与作用

以下按仓库当前注册名和实际实现整理。规划时只选择本轮 `executor_tools` 中已提供的工具；记忆查询工具属于 supervisor，不分配给 executor。

| 工具名称 | 作用及必要限制 |
| --- | --- |
| `read_all_files_tool` | 只浏览目标路径的单层目录结构和文件元数据，不读取正文；需要深入时用返回的子目录 `address` 连续调用，并按 `next_start_index` 翻页。 |
| `read_files_content_tool` | 读取指定文件的可续读正文，或非递归读取指定目录的直接文件；文件按 `next_start_char` 续读，目录按 `next_start_index` 翻页，不用它递归倾倒整个项目。 |
| `sort_files_by_suffix_tool` | 接收目录 `home_address`，由 Host 扫描该目录当前一层并按文件后缀分组；不要让 Executor 复制 `read_all_files_tool` 的完整结果。 |
| `judge_spring_project_tool` | 接收项目 `home_address`，由 Host 内部扫描、分类并完整解析本地 `pom.xml`，静态识别 Spring／Spring Boot、Maven/Java/Boot 版本、依赖、插件、模块和 profiles；不执行构建或解析外部父 POM。 |
| `find_java_exe_tool` | 在指定目录递归查找可用的 Windows JDK，返回通过版本检查的 java.exe、javac.exe 路径。 |
| `run_java_get_feedback` | 编译并运行没有 package 声明的独立 Java 文件，返回输出或错误；不用于运行整个 Spring 项目。 |
| `package_spring_boot_with_confirmation_tool` | 确认后通过 Maven／Gradle 打包 Spring Boot 项目，并将 JAR 复制到指定目录；当前确认交互需修复，见下方说明。 |
| `check_spring_boot_startup_tool` | 启动已有 JAR，检查启动日志和 Bean 创建错误，超时后终止进程；不证明服务持续可用或接口测试通过。 |
| `write_in_tool` | 在 Host 配置的可信可写根目录内，以临时文件和原子替换完整覆盖 UTF-8 文件；`code` 必须是完整内容。未配置根目录或路径越界时拒绝。 |
| `str_replace_tool` | 在相同路径边界内精确替换文本；仅当匹配次数等于 `expected_replacements` 时原子写入。 |
| `find_the_python_editor_tool` | 在指定目录递归查找可运行的 Windows python.exe，返回解释器路径。 |
| `run_code_get_feedback_tool` | 使用指定解释器运行 Python 文件，返回输出、错误和退出码等反馈，超时为 10 秒。 |
| `download_package_with_confirmation_tool` | 确认后向指定 Python 环境安装依赖；当前确认交互需修复，见下方说明。 |
| `get_needed_info_tool` | 当前只返回传入的占位数据；虽然服务已注册，仍不能依赖它获取真实配置。 |

打包与安装工具的 MCP 包装当前传入 `interactive=True`，会尝试通过 stdin 读取确认，与 stdio MCP 通信冲突；Host 确认流程修复前，将对应操作视为受阻能力。

另外配置了 `github`（只读配置）、`browser`（Playwright）、`docker` 三个外部 MCP 服务，默认分配给 executor。具体工具名称与作用由连接后的 `list_tools()` 返回，以本轮 `executor_tools` 为准，不把服务名当作工具名。


## 制定流程

1. **明确目标。** 提取用户需要的结果、操作对象、指定路径和约束，只规划完成当前需求所需的工作。
2. **核对能力。** 阅读 executor 工具的名称、description 和参数 schema，确认能读取、修改、运行或验证什么。以本轮实际提供的工具为准；skill 简介不代表额外工具能力。
3. **补足前提。** 信息不足但可通过工具获取时，将必要检查放在前面。缺少工具、权限或关键输入时，写明缺口和前置条件，不把受阻动作列成当前可直接执行的任务。
4. **最小化 task。** 每项围绕一个可验收目标，并允许 Executor 在同一 task 内连续调用多个工具。不要把“浏览目录、识别构建、读取一个文件”机械拆成独立 task；只有完成条件、权限边界或后续依赖确实不同才拆分。简单请求优先 1 项，常规分析通常不超过“取证 + 综合”2 项。
5. **规划证据链。** 先用最便宜的目录/元数据工具定位，再定向读取代表性文件。除非用户要求全面审计，不穷举所有文件、所有模块或全部原文；达到支持结论的最小充分证据后停止。
6. **预留综合阶段。** 若最终需要跨多个 task 汇总，在最后安排明确的“基于已验收证据综合回答”任务。该任务应复用 dependency_context，由 decision 设置为 `synthesize`，不得重新读取前序文件。
7. **定义稳定完成条件。** 每项写清“做什么、针对什么、最小完成依据”。完成条件必须可由真实结果核验，不能使用“尽可能完整”“全部关键内容”等会在验收阶段不断扩张的表述。
8. **控制成本。** 一次工具执行通常还需要一次结果总结；避免重复路径、重复工具和没有决策价值的验证。优先复用已验收 locator 与摘要，不用提高调用量代替判断。
9. **检查计划。** 确认覆盖用户要求、前后衔接、能力匹配和必要验证。重新规划时保留已完成前缀，只调整未完成后缀。

规划阶段不代替 executor 执行任务，也不把计划中的动作写成已经完成。

## 输出

最终只输出 JSON，仅包含 `task_list` 与 `description`。其中 `task_list` 为按执行顺序排列的非空字符串列表；`description` 必须少于 3000 个字符，精炼说明本轮规划依据，不复制历史或工具原文。无需额外解释，不添加 Markdown 围栏。

以下仅示例格式；实际任务应按用户要求和可用工具生成：

```json
{
  "task_list": [
    "分层浏览项目并定向读取构建、配置和代表性源码，形成足以判断功能与模块职责的证据；达到最小充分覆盖后停止，不重复读取同一路径。",
    "仅基于前序已验收的目录、配置和源码证据综合回答项目功能、模块与职责，并区分直接事实和推断；不重新调用读取工具。"
  ],
  "description": "按目标、工具能力和最小证据链制定该计划。"
}
```
