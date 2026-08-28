# AGENTS.md

## 换行符规范

- 发布包一律 LF 换行：打包前把 `.sh` 等文本转 LF。
- CI（`.github/workflows/ci.yml`）会校验仓库内文本无 CRLF。
- 本地校验：`powershell -File scripts/check_lf.ps1` 或 `bash scripts/check_lf.sh`。

