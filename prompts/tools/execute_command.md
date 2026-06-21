## execute_command

在终端执行 Windows 命令，受白名单和安全模式约束。

**白名单命令：** dir / copy / move / del / mkdir / rmdir / echo / type / findstr / where / python / pip / git / npm / node / systeminfo / wmic / winget / msiexec / taskkill

**确认模式：** msiexec /x 和 msiexec /uninstall 首次调用返回 confirmation_required，需用户确认后用 confirmed=true 重试。

**禁止模式：** rm -rf /、del /f /s、format、shutdown（代码层拦截，直接拒绝）

**路径规范：** 使用绝对路径，分隔符用反斜杠。优先用专用工具（scan_directory / move_file / copy_file / search_files），专用工具无法满足时才用本工具。

**编码与超时：** GBK（Windows 中文环境），超时 30 秒。
