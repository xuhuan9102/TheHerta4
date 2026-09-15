# 发布打包流程（**一律手动**）

**结论：不使用 CI 自动打包。** `.github/workflows/release.yml` 保留，但**只作为排除规则的
唯一来源**，不再依赖它出包。

理由（2026-09-15 查证，证据在 Actions 页面 Annotations）：tag 触发的 Release Package 运行
`#91`(v4.4.45)、`#92`(v4.4.46)、`#93`(v4.4.47) **全部 conclusion=failure**，原文

> The job was not started because your account is locked due to a billing issue.

这是 GitHub **账号计费锁定**导致 Actions 无法启动（不是仓库/工作流配置问题；历史
v4.4.44/45/46 的 Release 也都是手工建的）。所以在账号问题解决前（乃至之后，按用户裁定）
**发布包一律本地手工打、Release 一律手工上传**。

---

## 一次发布的完整步骤

1. **版本号**（历次发布提交都只改这 2 个文件、2 行）
   - `__init__.py` → `"version": (4, 4, XX)`
   - `ui/ui_panel_basic.py` → `layout.label(text="TheHerta4 v4.4.XX", icon='INFO')`
2. **提交**：`chore(release): v4.4.XX`
3. **打标签**（annotated；主题 `TheHerta4 vX.Y.Z`，正文写本次发布摘要）
   ```powershell
   git tag -a v4.4.XX -F "$env:TEMP\tag.txt"
   ```
4. **推送**：`git push origin main` 然后 `git push origin v4.4.XX`
   （tag 仍会触发一次注定失败的 CI 运行；不影响发布）
5. **打包**
   ```powershell
   python tools/package_release.py            # 版本号自动读 __init__.py
   ```
   → `dist/TheHerta4-4-4-XX.zip`，并打印 **bytes / entries / sha256**（抄进下一步的说明）
6. **写更新说明**：`dist/TheHerta4-4-4-XX-更新说明.md`
   （格式照 `dist/TheHerta4-4-4-44|45|46|47-更新说明.md`：区间统计 / 版本号 / 标签 / 产物三项 /
   改了什么 / 升级须知 / 工程与验证 / 已知遗留）
7. **手工建 GitHub Release**：网页 Release 页面 → 选标签 `v4.4.XX` → 上传 zip →
   正文粘更新说明要点 + `sha256`。
   本机 `gh` **未登录**（`gh auth status` 报 not logged into any GitHub hosts）；
   若已 `gh auth login`，可代劳：
   ```powershell
   gh release create v4.4.XX dist/TheHerta4-4-4-XX.zip --title "TheHerta4 v4.4.XX" --notes-file dist/TheHerta4-4-4-XX-更新说明.md
   ```

---

## 包内容规则（`tools/package_release.py` 已自动化）

脚本**解析** `.github/workflows/release.yml` 里的 `--exclude='…'` 名单，所以规则只有一处：

- 只取 **git 跟踪**文件（等价 CI 的 `actions/checkout`；未跟踪的 `.dbg/`、`.review-out/`、
  `reports/` 等本地残留天然不入包）；
- 顶层套一层 `TheHerta4/`（Blender 可直接 Install from Disk）；
- 目录/同名排除（26 项）：`.git` `.github` `.agents` `.trae` `.venv` `.codex_benchmarks`
  `.codegraph` `.dbg` `.kunsdd` `__pycache__` `docs` `dist` `DIST` `Logs` `references`
  `reports` `test` `tests` `tools` `theherta4_updater`
- 通配排除（6 项）：`*.pyc` `*.pyo` `*.bak-*` `*.GOOD-*` `*.log` `*.md`（外加
  `.gitignore` `skills-lock.json` `README*.md` `requirements.txt`，与 `*.md` 重叠）

> 注意：`docs/` 与 `tools/` 都在排除名单里 —— 本文件与打包脚本**不会**进发布包。

## 校验

```powershell
python tools/package_release.py --check        # 只列规则与入包文件数（当前 480 跟踪 → 324 入包）
python -m pytest tests -q                      # 全量测试（当前 1888 passed / 19 skipped）
```

包内要点自查：外层目录只有 `TheHerta4`、`TheHerta4/__init__.py` 存在、无 `.md`/`tests`/
`docs`/`__pycache__`/`dist` 条目。脚本每次都会打印 `bytes / entries / sha256` 三项，
与 `dist/` 里的历史包同格式对得上（例：v4.4.47 = 4,832,113 字节 / 324 条目 /
`aca14f50f6a51a23ef137d24f510fffb5b1a25065c4695ce6fb940bbd65da9d5`）。
