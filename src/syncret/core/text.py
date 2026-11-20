import unicodedata


# untrusted text made safe to show: control, format and line-separator chars become visible
# <U+XXXX> markers, so a skill cannot forge report lines, send escape codes or reorder text;
# keep_newlines leaves line breaks in multi-line text such as diffs
def shown(text, keep_newlines=False):
    keep = "\t\n" if keep_newlines else "\t"
    return "".join(
        f"<U+{ord(c):04X}>" if c not in keep and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
        else c
        for c in str(text)
    )
