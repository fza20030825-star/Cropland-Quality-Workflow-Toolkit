# Cropland Quality Workflow Toolkit

Cropland Quality Workflow Toolkit 是一个面向 ArcGIS Pro Python 环境的耕地质量评价流程工具集，用 Tkinter 图形界面封装了 5 个连续处理步骤。工具重点解决高标准农田项目区、第二步评价结果、现有农田面、最新地类图斑、最新行政区数据和官方面积统计数据之间的字段匹配、坐标系统一、空间叠置赋值、面积平差、完整性审计和可追溯日志问题。

## 运行环境

请使用 ArcGIS Pro 的 Python 环境或其克隆环境运行，例如：

```text
C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3
```

如果你已经克隆到 Conda 环境，也可以使用自己的克隆环境路径。

必须具备：

- `arcpy`：由 ArcGIS Pro Python 提供，不能通过普通 `pip` 安装。
- `tkinter`：通常随 Python 自带，用于图形界面。
- `openpyxl`：第二步读取规则 Excel 时使用。

可选检查：

```powershell
python run.py --check-env
```

## 快速开始

在 VS Code 或 PowerShell 中进入项目根目录：

```powershell
cd "path\to\cropland-quality-workflow-toolkit"
$env:PYTHONPATH="src"
```

说明：对外项目名和仓库名使用 `Cropland Quality Workflow Toolkit` / `cropland-quality-workflow-toolkit`；内部 Python 包名暂时保留 `cropland_quality_update`，用于保持现有入口脚本和导入路径稳定。

按顺序运行 5 个入口：

```powershell
python run_merge_shp_ui.py
python run_membership_ui.py
python run_update_scores_ui.py
python run_update_land_blocks_ui.py
python run_area_balance_ui.py
```

## 五个工具做什么

| 步骤 | 入口文件 | 主要作用 |
| --- | --- | --- |
| 1 | `run_merge_shp_ui.py` | 合并多个高标准农田项目区面矢量，检查坐标、字段和空间重叠，输出新图层。 |
| 2 | `run_membership_ui.py` | 按国标二级区规则表计算各指标隶属度、评价得分和质量等级。 |
| 3 | `run_update_scores_ui.py` | 将第二步结果更新到现有农田面输出副本中，输出固定 39 个字段。 |
| 4 | `run_update_land_blocks_ui.py` | 将第三步结果更新到最新耕地图斑中，只处理 `地类编码=0101/0102/0103` 的农田，输出固定 39 个字段。 |
| 5 | `run_area_balance_ui.py` | 按 2024 年湖北省耕地面积统计数据对 `平差面积` 做分地类平差，新增 `等级*面积`，并在界面和日志输出县域加权平均质量等级。 |

输入文件不会被原地修改。所有工具都会生成单独输出结果；如果输出路径等于输入数据本身，工具会拒绝执行。

## 强烈建议使用 GDB

强烈建议用户把过程结果和最终结果都保存为 FileGDB 面要素类，而不是 Shapefile。Shapefile/DBF 对中文字段名、字段长度、字段类型和编码都有明显限制，容易造成字段截断、重名、乱码或数值精度损失。第三步、第四步和第五步已经强制使用 GDB；第一步和第二步即使界面允许输出 Shapefile，也建议仅在临时交换或额外兼容导出时使用，正式流程请优先保存到 GDB。

## 规则表

第二步规则 Excel 放在：

```text
data/rules
```

当前包含 5 个国标二级区规则表。运行第二步时，工具会按用户选择的国标二级区读取对应规则文件。

## 输出和日志

默认输出目录：

```text
outputs
```

日志目录：

```text
outputs/logs
```

运行过程文件目录：

```text
outputs/process_files
```

日志和过程文件是本地运行产物，已经在 `.gitignore` 中忽略。每个工具在任务结束后会尽量清理过程文件；即使任务失败，也会记录失败原因和审计报告。

## 详细流程说明

专业核查和交付前复核请看：

```text
docs/workflow_manual.qmd
```

这个文件详细说明了每个步骤的入口、输入、输出、字段规则、坐标规则、更新判断标准、失败/打回条件、日志内容和质量审计机制。

## 测试

普通 Python 环境不能真正运行 ArcPy 空间处理，但可以做基础导入和路径安全测试。建议在 ArcGIS Pro Python 环境中执行：

```powershell
$env:PYTHONPATH="src"
python -m pytest
```

## GitHub 上传前注意

- 不提交本地 `.env`、日志、过程文件、GDB、Shapefile 和缓存。
- 不提交真实涉密或大体量生产数据；示例数据如需发布，应脱敏后单独说明。
- `data/rules` 中的规则 Excel 是工具运行所需的固定规则资源，默认保留在仓库中。
