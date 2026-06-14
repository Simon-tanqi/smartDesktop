## write_file

**功能**：将内容写入文件。自动创建不存在的父目录。路径限制在 work_space 内。

**何时使用**：
- 生成新代码文件
- 创建配置文件、README 等文档
- 将处理结果保存为文件

**何时不使用**：
- 修改已有文件的某几行 → 用 replace_in_file（精准替换，避免重写整个文件）
- 需要先看文件内容再决定 → 先用 read_file 读取
- 目标路径在 work_space 外 → 需要先告知用户限制

**参数**：
- file_path: str（必填）文件绝对路径，必须在 work_space 目录内
- content: str（必填）要写入的内容，原样写入不转义
- overwrite: bool（默认 true）文件已存在时是否覆盖

**关键约束**：
- 路径必须在 work_space 内，否则拒绝写入（安全限制）
- 父目录不存在时自动创建
- 内容以 UTF-8 编码写入
- overwrite=false 且文件已存在时拒绝操作

**错误处理**：
- 路径不在 work_space 内 → `{"success": false, "message": "非法路径！只能在 work_space 内操作: ..."}`
- 文件已存在且 overwrite=false → 返回冲突提示
- 写入失败（权限/磁盘满） → 返回异常信息
