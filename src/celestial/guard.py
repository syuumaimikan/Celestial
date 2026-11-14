"""全自動のエージェントが実行するコマンドとコードの安全の確かめ(Claude Code の自動モードのように、危ない操作だけを止める)。

判定は3段階:
- "block": PC 全体を壊す・PC の設定を変える・記録を消す・隠したコードを実行するなど、取り返しがつかない操作。
           確認があっても実行しない。
- "ask":   作業フォルダの外を変える、ネットから取り込む・公開する、プログラムを止める、秘密の情報に触れるなど。
           利用者に確かめる(確かめる手段が無ければ実行しない)。
- "ok":    作業フォルダの中で完結する普通の操作(テスト・ビルド・git status・スクリプトの実行など)。

小さいAIの勘違いによる事故を防ぐためのもので、巧妙に隠した悪意のあるコマンドを完全に見抜くものではない。
(ファイルを読む・書く道具は、もともと作業フォルダの外に届かない。)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

OK, ASK, BLOCK = "ok", "ask", "block"

_I = re.I
# ---- 取り返しがつかない操作(実行しない) ----
_BLOCK = [
    (re.compile(r"\bformat(\.com)?\s+[a-z]:", _I), "ドライブのフォーマット"),
    (re.compile(r"\b(diskpart|fdisk|mkfs(\.\w+)?|Clear-Disk|Initialize-Disk|Format-Volume|Remove-Partition|Set-Partition)\b", _I),
     "ディスクの構成の変更"),
    (re.compile(r"\bdd\b[^|;&\n]*\bof=/dev/", _I), "ディスクへの直接の書き込み"),
    (re.compile(r"\b(shutdown(\.exe)?|Restart-Computer|Stop-Computer|logoff|poweroff|reboot|halt)\b", _I), "PC の再起動・電源・サインアウト"),
    (re.compile(r"\brundll32\b[^\n]*\b(user32|powrprof)\b", _I), "PC の電源・ロックの操作"),
    (re.compile(r"\breg(\.exe)?\s+(add|delete|import|load|unload|restore|copy|save)\b", _I), "レジストリの変更"),
    (re.compile(r"\bregedit\b", _I), "レジストリの変更"),
    (re.compile(r"\b(Set|New|Remove|Rename|Clear)-Item(Property)?\b[^\n]*\b(HKLM|HKCU|HKCR|HKU|HKCC|Registry)::?", _I), "レジストリの変更"),
    (re.compile(r"\b(bcdedit|bootrec|bcdboot)\b", _I), "起動の設定の変更"),
    (re.compile(r"\bvssadmin\b[^\n]*\b(delete|resize)\b|\bwbadmin\s+delete\b|\bcipher(\.exe)?\s+/w\b", _I), "バックアップ・消したデータの消去"),
    (re.compile(r"\bwevtutil(\.exe)?\s+(cl|clear-log)\b|\bClear-EventLog\b", _I), "PC の記録の消去"),
    (re.compile(r"\b(Set|Add|Remove)-MpPreference\b|\bnetsh(\.exe)?\s+(adv)?firewall\b|\b(Set|Disable|Remove)-NetFirewall\w*\b"
                r"|\bSet-ExecutionPolicy\b", _I), "セキュリティの設定の変更"),
    (re.compile(r"\b(takeown|icacls|cacls)\b|\battrib\b[^\n]*\+[sh]", _I), "ファイルの所有者・権限の変更"),
    (re.compile(r"\bnet(\.exe)?\s+(user|localgroup|accounts)\b|\b(New|Remove|Set|Disable)-LocalUser\b|\bAdd-LocalGroupMember\b", _I),
     "ユーザーアカウントの変更"),
    (re.compile(r"\bsc(\.exe)?\s+(delete|create|config|failure)\b|\b(New|Remove|Set)-Service\b|\bschtasks\b"
                r"|\b(Register|Unregister|Set)-ScheduledTask\b", _I), "サービス・タスクの登録や変更"),
    (re.compile(r"\bpowershell(\.exe)?\b[^\n]*\s-e(nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{16,}|\bpwsh\b[^\n]*\s-e(nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{16,}", _I),
     "中身を隠したコマンド"),
    (re.compile(r"FromBase64String[^\n]*\b(iex|Invoke-Expression)\b|\|\s*(iex|Invoke-Expression)\b", _I), "取り込んだ文字をそのまま実行"),
    (re.compile(r"\b(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(sh|bash|zsh|python3?|py|powershell|pwsh|cmd)\b", _I),
     "ネットから取り込んだものをそのまま実行"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};\s*:"), "PC を止めるコマンド"),
    (re.compile(r"\brm\s+(?:-{1,2}[\w-]+\s+)*(?:/|/\*|~|~/|~/\*|\$HOME|\$\{HOME\})/?(?=\s|$|;|&|\|)"), "ドライブ全体・ホームフォルダ全体の削除"),
]

# ---- 確かめてから行う操作 ----
_NET = re.compile(
    r"\b(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod|Start-BitsTransfer|bitsadmin|certutil\b[^\n]*-urlcache)\b"
    r"|\bgit\s+(clone|fetch|pull|submodule\s+update|lfs\s+pull)\b"
    r"|\b(pip3?|uv|poetry|conda|pipx)\s+(install|add|sync)\b|\bpython3?\s+-m\s+pip\s+install\b|\bpy\s+-m\s+pip\s+install\b"
    r"|\b(npm|pnpm|yarn|bun)\s+(install|i|add|ci|update|upgrade)\b|\bnpx\b|\bbunx\b"
    r"|\b(winget|choco|scoop)\s+(install|upgrade)\b|\bcargo\s+(install|add|update)\b|\bgo\s+(get|install)\b"
    r"|\bdotnet\s+(add|restore)\b|\b(flutter|dart)\s+pub\s+(get|add|upgrade)\b|\bgem\s+install\b|\bcomposer\s+(install|require)\b",
    _I)
_PUBLISH = re.compile(
    r"\bgit\s+push\b|\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bgh\s+\w+\s+(create|delete|edit|merge|close|upload)\b"
    r"|\b(scp|sftp|ftp|ssh|telnet)\b|\brsync\b[^\n]*:", _I)
_KILL = re.compile(r"\b(taskkill|Stop-Process|spps|kill|pkill|killall)\b", _I)
_ELEVATE = re.compile(r"\b(runas|sudo|gsudo)\b|-Verb\s+RunAs\b", _I)
_ENV = re.compile(r"\bsetx\b|\[Environment\]::SetEnvironmentVariable|\bSet-ItemProperty\b[^\n]*Environment", _I)
_SECRET = re.compile(r"[\\/]\.ssh\b|\bid_(rsa|ed25519|ecdsa|dsa)\b|[\\/]\.aws\b|[\\/]\.git-credentials\b|[\\/]\.netrc\b|"
                     r"\bLogin Data\b|\bCookies\b|\\User Data\\|\bwallet(\.dat)?\b|\bkeychain\b|\bcredentials?\.(json|xml|txt)\b", _I)
# 変更する(書く・消す・動かす)コマンドの目印
_WRITE_VERB = re.compile(
    r"\b(rm|rmdir|rd|del|erase|Remove-Item|ri|rni|move|mv|Move-Item|mi|ren|rename|Rename-Item|copy|cp|xcopy|robocopy|Copy-Item|cpi"
    r"|Set-Content|Add-Content|Out-File|Clear-Content|New-Item|ni|mkdir|md|tee|truncate|shred|chmod|chown|ln|mklink)\b"
    r"|(?<![<>=!-])>{1,2}(?!=)", _I)
_ABS_PATH = re.compile(r"(?<![\w/\\.])(?:[A-Za-z]:[\\/][^\s\"'|;&<>`]*|\\\\[^\s\"'|;&<>`]+|~[\\/][^\s\"'|;&<>`]*|~(?=\s|$)"
                       r"|/(?:mnt|cygdrive)/[a-z]/[^\s\"'|;&<>`]*|/[a-z]/[^\s\"'|;&<>`]*)")
_QUOTED_ABS = re.compile(r"[\"']((?:[A-Za-z]:[\\/]|\\\\|~[\\/])[^\"']*)[\"']")
_VAR_PATH = re.compile(r"%(USERPROFILE|HOMEPATH|APPDATA|LOCALAPPDATA|PROGRAMDATA|PROGRAMFILES|SYSTEMROOT|WINDIR|PUBLIC|TEMP|TMP)%"
                       r"|\$env:(USERPROFILE|HOMEPATH|APPDATA|LOCALAPPDATA|PROGRAMDATA|ProgramFiles|SystemRoot|windir|PUBLIC|TEMP|TMP)\b"
                       r"|\$HOME\b|\$\{HOME\}", _I)
_SYSTEM_ROOTS = re.compile(r"^(?:[a-z]:[\\/]?|[a-z]:[\\/](windows|program files( \(x86\))?|programdata|users|system volume information)[\\/]?"
                           r"|/|~|/[a-z]/?|/(mnt|cygdrive)/[a-z]/?)$", _I)
_PARENT = re.compile(r"(?:^|[\s\"'=(])\.\.(?:[\\/]|\s|$)")
_CD_OUT = re.compile(r"\b(cd|chdir|Set-Location|sl|pushd)\s+(/d\s+)?[\"']?([A-Za-z]:[\\/]?|\\\\|~|/|\.\.)", _I)

# Python のコードで気をつける書き方
_PY_DELETE = re.compile(r"\b(?:shutil\.rmtree|shutil\.move|os\.(?:remove|unlink|rmdir|removedirs|rename|replace))\b|\.(?:unlink|rmdir)\(")
_PY_SYSTEM = re.compile(r"\bwinreg\b|\bctypes\.windll\b|\b_winreg\b|\bos\.system\b|\bsubprocess\b|\bos\.popen\b|\bos\.exec\w*\b|\bos\.spawn\w*\b")
_PY_NET = re.compile(r"\b(urllib\.request|urlopen|requests\.|http\.client|socket\.|ftplib|smtplib|aiohttp|httpx)\b")
_URL = re.compile(r"\bhttps?://(\[[^\]]+\]|[^/\s:\"'`]+)", re.I)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "0.0.0.0"}
_PY_STRINGS = re.compile(r"(?:[rbuRBU]{0,2})(\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*')")


def only_local_urls(text: str) -> bool:
    """文中の URL がすべてこの PC の中(localhost など)を指すか。URL が1つも無ければ False。"""
    hosts = [m.group(1).lower() for m in _URL.finditer(text)]
    return bool(hosts) and all(h in _LOCAL_HOSTS for h in hosts)


def _inside(path: str, workspace: Path | None) -> bool:
    """パスが作業フォルダの中か。"""
    if workspace is None:
        return False
    text = os.path.expandvars(os.path.expanduser(path.strip().strip("\"'")))
    m = re.match(r"^/(?:mnt/|cygdrive/)?([a-z])/(.*)$", text, re.I)  # Git Bash などの /c/... の形
    if m:
        text = f"{m.group(1)}:/{m.group(2)}"
    try:
        target = Path(text).resolve()
        root = workspace.resolve()
    except (OSError, ValueError):
        return False
    return target == root or root in target.parents


def _paths(text: str) -> list[str]:
    found = [m.group(1) for m in _QUOTED_ABS.finditer(text)]
    found += [m.group(0) for m in _ABS_PATH.finditer(text)]
    return [p.rstrip(".,)") for p in found if p]


def check_command(command: str, *, workspace: Path | None, allow_net: bool) -> tuple[str, str]:
    """シェルのコマンドを判定する。戻り値は (OK / ASK / BLOCK, 理由)。"""
    text = command.strip()
    if not text:
        return OK, ""
    for pattern, reason in _BLOCK:
        if pattern.search(text):
            return BLOCK, reason
    writes = bool(_WRITE_VERB.search(text))
    if writes:
        for path in _paths(text):
            if _SYSTEM_ROOTS.match(path.strip("\"'")):
                return BLOCK, f"ドライブやシステムのフォルダ({path})の変更"
    if _ELEVATE.search(text):
        return ASK, "管理者の権限での実行"
    if _SECRET.search(text):
        return ASK, "鍵・パスワード・ブラウザーの記録など、秘密の情報への接触"
    if _PUBLISH.search(text):
        return ASK, "外のサーバーへの送信・公開"
    if _KILL.search(text):
        return ASK, "実行中のプログラムを止める"
    if _ENV.search(text):
        return ASK, "PC 全体の環境変数の変更"
    if writes:
        outside = [p for p in _paths(text) if not _inside(p, workspace)]
        if outside:
            return ASK, f"作業フォルダの外({outside[0][:80]})の変更"
        if _VAR_PATH.search(text):
            return ASK, "作業フォルダの外(ユーザーやシステムのフォルダ)の変更"
        if _PARENT.search(text) or _CD_OUT.search(text):
            return ASK, "作業フォルダの外へ出てからの変更"
    if _NET.search(text) and not allow_net and not only_local_urls(text):
        return ASK, "ネットからの取り込み(ネットの利用が許可されていません)"
    return OK, ""


def check_python(code: str, *, workspace: Path | None, allow_net: bool) -> tuple[str, str]:
    """AIが書いた Python のコードを判定する。"""
    if _PY_SYSTEM.search(code):
        commands = [s.strip("\"'") for s in _PY_STRINGS.findall(code)]
        for literal in commands:  # コードの中から実行するコマンドも、コマンドと同じ決まりで確かめる
            level, reason = check_command(literal, workspace=workspace, allow_net=allow_net)
            if level != OK:
                return level, reason
        if re.search(r"\bwinreg\b|\b_winreg\b|\bctypes\.windll\b", code):
            return ASK, "PC の設定やシステムの機能を直接使うコード"
    literals = [s.strip("\"'") for s in _PY_STRINGS.findall(code)]
    if _PY_DELETE.search(code) or re.search(r"open\([^)]*[\"'][wax]\+?b?[\"']", code) or "write_text" in code or "write_bytes" in code:
        for literal in literals:
            for path in _paths(literal) or ([literal] if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/)", literal) else []):
                if _SYSTEM_ROOTS.match(path) and _PY_DELETE.search(code):
                    return BLOCK, f"ドライブやシステムのフォルダ({path})の削除"
                if not _inside(path, workspace):
                    return ASK, f"作業フォルダの外({path[:80]})の変更"
        if re.search(r"Path\.home\(\)|expanduser\(|os\.environ\[?[\"'](USERPROFILE|HOME|APPDATA)", code):
            return ASK, "作業フォルダの外(ユーザーのフォルダ)の変更"
    if _SECRET.search(code):
        return ASK, "鍵・パスワード・ブラウザーの記録など、秘密の情報への接触"
    if _PY_NET.search(code) and not allow_net and not only_local_urls(code):
        return ASK, "ネットへの接続(ネットの利用が許可されていません)"
    return OK, ""
