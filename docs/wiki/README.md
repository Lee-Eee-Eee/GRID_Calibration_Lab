# 工作台教程

[在线 Wiki](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki) · [项目首页](../../README.md)

| 页面 | 内容 |
| --- | --- |
| [教程首页](Home.md) | 阅读顺序、首次跑通、数据 / 预设 / 试算 / TB 包的关系 |
| [安装与连接](Server-Setup.md) | 个人服务器环境、共享目录软链接、服务、SSH 转发和更新 |
| [数据与预设](Data-and-Presets.md) | 目录、载荷、XML、换算、文件规则、四份 YAML 与版本复用 |
| [TB 温偏标定](TB-Calibration.md) | 解包、通道、拖窗、局部拟合、曲面、三维残差与屏蔽恢复 |
| [EC 能量标定](EC-Calibration.md) | 选择 TB 包、校正、多峰、Co60 联合拟合、刻度和分辨率 |
| [复核与结果](Review-and-Results.md) | 保存、比较、JSON / 图预览、运行记录及重开恢复 |
| [常见问题](Troubleshooting.md) | 连接、配对、CRC、窗口、拟合与依赖排查 |

## 维护教程

本目录保存 Wiki 的 Markdown 源文件，随实现一同审查和提交。修改正文时同步发布到 Wiki Git 仓库：

```bash
git clone https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab.wiki.git ../GRID_Calibration_Lab.wiki
```

将本目录除 `README.md` 外的 Markdown 文件复制到 Wiki 检出目录，提交并推送 Wiki 的默认分支。`Home.md` 为首页，`_Sidebar.md` 和 `_Footer.md` 提供导航。页面间链接使用完整 Wiki URL，便于同时从仓库正文访问。后续更新可以直接复用该 Wiki 检出目录。

新增或修改按钮说明时，核对实际 UI 标签；修改安装说明时，在新个人目录验证命令；更新 Co60 等科学配置时，写明适用的数据、模型与版本创建方法。
