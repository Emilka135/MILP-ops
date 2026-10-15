import os
from pathlib import Path
for name in ("ari.conf", "pjsip.conf"):
    path=Path("/etc/asterisk")/name
    text=path.read_text()
    for var in ("ARI_PASSWORD", "SIP_PHONE_PASSWORD", "SIP_OPERATOR_PASSWORD"):
        if "${"+var+"}" in text:
            value=os.environ.get(var, "")
            if len(value)<12 or any(c in value for c in "\r\n;"):
                raise ValueError(var+" must contain at least 12 characters and no config delimiters")
            text=text.replace("${"+var+"}", value)
    path.write_text(text)
os.execvp("asterisk", ["asterisk", "-f", "-vv"])
