# AGENTS.md

## Git 协作约定

本仓库使用两个远程：

- `origin`：`https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab.git`
- `tsinghua`：`git@git.tsinghua.edu.cn:litao24/GRID_Calibration_Lab.git`（SSH 认证；项目页面：`https://git.tsinghua.edu.cn/litao24/GRID_Calibration_Lab`）

用户说“push”时，默认将**当前本地分支**以同名分支分别推送到 `origin` 和 `tsinghua`。

1. 先检查当前分支、已有改动和两个远程的最新历史。远端同名分支存在时，确认其提交已包含在本地待推送历史中；有远端独有提交时先核对并整合。远端没有同名分支时可以新建。
2. 分别执行普通推送，使用明确的同名 refspec：`git push origin HEAD:refs/heads/<当前分支>` 和 `git push tsinghua HEAD:refs/heads/<当前分支>`。不强推，不顺带推送其他分支或标签。
3. 保留已有上游关系，不使用 `-u` / `--set-upstream`，不修改 `branch.*.remote` 或 `branch.*.merge`。
4. 每端推送后，分别用 `git ls-remote --heads <远程> refs/heads/<当前分支>` 核对目标提交编号与本地 `HEAD` 相同。最终逐项报告两个远程的结果；只有两端都验证一致才算双远程同步完成。
5. 提交时只暂存本次任务文件，保留其他已有改动和未跟踪文件。

默认仍仅在本地提交；用户要求“push”时执行上述双远程同步。
