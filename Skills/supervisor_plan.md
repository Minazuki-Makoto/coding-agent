---
name: supervisor_plan
description: 根据用户需求、executor 当前实际可用的工具和 skill 简介，生成按依赖顺序排列、可执行且可验收的任务计划。
---

# Supervisor Plan

根据输入的用户问题和 `executor_tools` 制定计划；提供了 skill 目录时，结合其 name、description 选择适合的工作流程。

## Executor 工具名称与作用

以下按仓库当前注册名和实际实现整理。规划时只选择本轮 `executor_tools` 中已提供的工具；记忆查询工具属于 supervisor，不分配给 executor。

| 工具名称 | 作用及必要限制 |
| --- | --- |
| `read_all_files_tool` | 只浏览目标路径的单层目录结构和文件元数据，不读取正文；需要深入时用返回的子目录 `address` 连续调用，并按 `next_start_index` 翻页。 |
| `read_files_content_tool` | 读取指定文件的可续读正文，或非递归读取指定目录的直接文件；文件按 `next_start_char` 续读，目录按 `next_start_index` 翻页，不用它递归倾倒整个项目。 |
| `sort_files_by_suffix_tool` | 将读取结果按文件后缀分组，辅助识别源码和构建配置。 |
| `judge_spring_project_tool` | 完整解析本地 `pom.xml`（不受目录扫描预览截断影响），静态识别 Spring／Spring Boot、Maven/Java/Boot 版本、依赖、插件、模块和 profiles；不执行构建或解析外部父 POM。 |
| `find_java_exe_tool` | 在指定目录递归查找可用的 Windows JDK，返回通过版本检查的 java.exe、javac.exe 路径。 |
| `run_java_get_feedback` | 编译并运行没有 package 声明的独立 Java 文件，返回输出或错误；不用于运行整个 Spring 项目。 |
| `package_spring_boot_with_confirmation_tool` | 确认后通过 Maven／Gradle 打包 Spring Boot 项目，并将 JAR 复制到指定目录；当前确认交互需修复，见下方说明。 |
| `check_spring_boot_startup_tool` | 启动已有 JAR，检查启动日志和 Bean 创建错误，超时后终止进程；不证明服务持续可用或接口测试通过。 |
| `write_in_tool` | 创建父目录并写入文本；当前实际使用追加模式，不能作为覆盖文件或局部补丁工具。 |
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
4. **按依赖拆分。** 每项围绕一个明确目标，可包含多次工具调用。简单任务可以只有一项；复杂任务按实际依赖拆分，避免重复和无关步骤。
5. **定义完成条件。** 每项写清“做什么、针对什么、完成依据”。涉及外部操作时注明拟用的真实工具名；具体参数可由 executor 根据执行时的信息确定。
6. **检查计划。** 确认步骤覆盖用户要求、前后衔接、工具能力匹配，并包含必要验证。重新规划时保留仍有效的已完成工作，只调整受新问题影响的部分。

规划阶段不代替 executor 执行任务，也不把计划中的动作写成已经完成。

## 输出

最终只输出 JSON，仅包含 `task_list` 与 `description`。其中 `task_list` 为按执行顺序排列的非空字符串列表；`description` 精炼说明本轮规划依据。无需额外解释，不添加 Markdown 围栏。

以下仅示例格式；实际任务应按用户要求和可用工具生成：

```json
{
  "task_list": [
    "使用 read_all_files_tool 分层浏览用户指定项目目录，定位入口、构建文件和主要模块；避免对同一路径重复读取。",
    "使用 read_files_content_tool 定向读取上一阶段发现的关键源码；完成依据为实际文件内容能够支持模块职责与调用关系，再整理执行流程并标明尚未确认的内容。"
  ],
  "description": ""
}
```
