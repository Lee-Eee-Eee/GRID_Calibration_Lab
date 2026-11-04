# GRID Calibration Lab 使用教程

GRID Calibration Lab 将天格探测器的温偏标定、能量标定、原谱复核和局部重算放在同一工作台中。计算在服务器运行，成员在自己的浏览器中通过 SSH 转发访问。

目前可直接按本教程使用 **14B / 15B 的 cali_proc 0.5 处理链**。安装使用 `feat/calibration-workbench` 分支。Python 导入名保持 `calibration_lib`。

## 从这里开始

| 你的目标 | 阅读页面 |
| --- | --- |
| 第一次安装、启动，或连接服务器工作台 | [服务器安装与连接](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/Server-Setup) |
| 选数据、继续任务、配置 XML、文件规则和源能线 | [数据目录、读取预设与任务](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/Data-and-Presets) |
| 为各层选共享或个人结果、局部替换输入、更新依赖 | [分层输入与结果复用](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/blob/feat/calibration-workbench/docs/layer_inputs.md) |
| 解包温偏实验、拖窗拟合、计算与复核曲面 | [TB 温度—偏压标定](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/TB-Calibration) |
| 选择 TB 包、拟合多能峰、计算能量刻度与分辨率 | [EC 能量标定](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/EC-Calibration) |
| 保存、比较、找 JSON / 图和运行日志、重开继续 | [复核、保存与结果查找](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/Review-and-Results) |
| 处理连接、配对、CRC、窗口或依赖问题 | [常见问题](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/Troubleshooting) |

## 第一次跑通一条谱

1. 按[安装教程](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/Server-Setup)建立自己的环境和工作区，登记共享数据，启动服务并建立 SSH 转发。
2. 在 **选择数据 / 产品** 选择载荷、目录和读取预设版本，继续已有任务，或创建一份有明确用途的新任务。
3. 先在 **TB · EXP/L0** 解包一个工况，进入 **L1**，选择通道，拖动拟合窗并点击 **按当前窗口拟合**。
4. 确认这条谱的峰和本底正确，再处理其余工况并计算 **TB L2**。在三维曲面或残差图点击点，返回对应原谱复核。
5. 做 EC 时，在 **corr** 的 **TB L2 → 更换** 中选择可用参数包并应用；共享产品、当前任务和其他任务均可作为来源。
6. 按 **EC EXP/L0 → corr → L1 → L2** 处理一个文件，再扩展到整批。放射源的每条能线都有自己的能量、窗口与中心锚，详见[多峰操作](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/EC-Calibration#4-一张放射源谱拟合多个峰)。
7. 在 **文件与运行记录** 查看刚生成的文件、JSON / 图预览和日志，保存修订。重新进入同一任务继续分析。

先处理一条谱能尽早确认文件配对、物理通道、曝光、TB 依赖与峰窗口；计算全量前再检查处理范围是否为“全部数据”。

已有分层结果时，可从目标层的输入栏接入对应上游。例如，直接在 TB L2 选择一份已有 L1 来计算曲面；要直接使用现成曲面，则打开 **结果 → 采用已有结果…**。

## 工作台中的四个选择

| 选择 | 决定什么 |
| --- | --- |
| 数据集 | 从哪一批原始数据与已有产品开始 |
| 读取预设及版本 | 如何解包、换算字段、匹配文件、选段和拟合 |
| 任务 | 本轮窗口、屏蔽点、配置快照、计算记录与结果保存在哪里 |
| TB L2 参数包 | EC 使用哪一套温偏响应参数 |

“新规范回归”“Co60 双峰 v2”是现有任务的用途名称，版本和算法以任务内选定的配置为准。不同成员的预设版本独立保存。15B Co60 双峰配置需要在自己的预设中建立，操作与示例见[联合双峰](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/EC-Calibration#15b-co60-联合双峰)。

## 图表与处理层

![TB 三维曲面与工况原谱](https://raw.githubusercontent.com/Lee-Eee-Eee/GRID_Calibration_Lab/feat/calibration-workbench/docs/images/tb-surface.jpg)

TB 使用 **EXP/L0 → L1 → L2**；EC 使用 **EXP/L0 → corr → L1 → L2**。导航切层用于查看，计算由对应按钮提交。谱拟合后更新曲面或能量关系，才会得到下游的新结果。

TB L2 可从实测点下钻到工况谱，也可切换三维残差。EC L2 默认使用双对数显示刻度点，保留线性坐标切换；点击能线点可查看真实谱。局部净谱与逐 bin 残差保留线性纵轴，以显示本底相减后的负值。

## 结果保存在哪里

共享输入位于服务器数据目录，个人工作区通过软链接登记它们。每份任务保存到 `<workspace>/runs/<run_id>/`，其中包含配置快照、TB 依赖、修订、产品和逐次计算日志。浏览器所在电脑不需要复制原始事件。

每层输入栏可选择 **共享产品 / 当前任务 / 其他任务**。共享产品来自登记的产品目录，也可将有读取权限的个人产品目录登记为来源；“其他任务”列出当前工作区同一物理载荷的已保存任务。

选择具体输入后保存引用；局部替换可展开 **选择文件与通道**。EC 的 TB 包独立选择，也可 **跟随本任务 TB**。修改配置或上游后，查看 **更新范围** 并使用 **更新受影响层**。完整操作见[分层输入指南](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/blob/feat/calibration-workbench/docs/layer_inputs.md)。

## 进一步阅读

- [项目 README](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/blob/feat/calibration-workbench/README.md)：安装入口、功能概览和脚本使用。
- [处理与数据指南](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/blob/feat/calibration-workbench/docs/processing.md)：处理器、Python API 和输出结构。
- [新规范适配验收](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/blob/feat/calibration-workbench/docs/contract_migration_validation.md)：14B / 15B 的真实数据回归。
- [Co60 双峰复核](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/blob/feat/calibration-workbench/docs/co60_doublet_validation.md)：峰身份、联合拟合与新旧残差。
- [反馈问题](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/issues)：附任务、层级、文件、通道和运行记录中的错误。
