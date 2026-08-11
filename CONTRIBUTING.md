# Contributing to smartDesktop

感谢您对 smartDesktop 的关注与贡献。请遵循以下约定，让协作更顺畅。

## 开发环境

- Python 3.11+
- 安装运行依赖：`pip install -r requirements.txt`
- 安装开发依赖：`pip install -r requirements-dev.txt`
- 安装 Git Hooks：`pre-commit install`

## 分支与提交

- 主分支：`main`（开发与发布均基于此分支）
- 功能分支命名建议：`feat/<简短描述>`、`fix/<简短描述>`
- Commit Message 建议遵循 [Conventional Commits](https://www.conventionalcommits.org/)：
  - `feat: 新增 XX 能力`
  - `fix: 修复 XX 问题`
  - `refactor: 重构 XX 模块`
  - `docs: 更新文档`
  - `test: 补充测试`

## 代码规范

- 提交前必须通过 `ruff` 检查：`ruff check .`
- 格式化：`ruff format .`
- 提交前必须通过语法编译检查：`python -m compileall -q .`
- 涉及核心逻辑改动时，应补充或更新 `tests/` 下的用例

## 运行时目录约定

- 项目源码与运行时数据严格分离：
  - 源码：仓库内
  - 运行时数据（日志 / 会话 / 记忆 / 数据库）：`%APPDATA%\smartDesktop\`
- 任何新增的日志或数据写入必须使用 `memory.config.DATA_DIR`，禁止写回项目目录
- 仓库内 `logs/`、`temp/`、`*.db` 等运行时产物已被 `.gitignore` 排除，不要提交

## 测试

- 运行全部测试：`python -m pytest tests/ -x -q`
- 新增功能建议同步提供测试用例，保持主干可用

## 提交流程

1. Fork 并创建功能分支
2. 完成开发并自测（lint + compile + test）
3. 提交 PR，描述改动内容与验证结果
4. 等待 Review 合并

## 问题反馈

- Bug / 建议请通过 GitHub Issues 提交，附上复现步骤、日志与运行环境

