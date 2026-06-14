## copy_file

**功能**：复制文件到目标位置，保留原文件元数据（修改时间等）。目标可以是目录或完整新路径。

**何时使用**：
- 备份文件到其他目录
- 复制模板文件用于新项目
- 将文件分发到多个位置

**何时不使用**：
- 不需要保留原文件 → 用 move_file
- 操作对象是目录 → 用 execute_command（xcopy/robocopy）
- 需要复制文件内容而非文件本身 → 用 read_file + write_file

**参数**：
- source: str（必填）源文件绝对路径
- destination: str（必填）目标目录或完整目标路径
- overwrite: bool（默认 true）目标已存在时是否覆盖

**关键约束**：
- 使用 shutil.copy2，保留文件元数据
- 目标为目录时自动拼接原文件名
- source 必须是已存在的文件

**错误处理**：
- 源文件不存在 → `{"success": false, "message": "源文件不存在: ..."}`
- 目标已存在且 overwrite=false → 返回冲突提示
- 磁盘空间不足 → 返回异常信息
