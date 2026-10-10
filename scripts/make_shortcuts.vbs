' ===================================================================
'  資材複合ツール(梱包資材総合ツール) ショートカットを作る
'
'  配布フォルダを配ったあと、その PC でこのファイルをダブルクリックすると、
'  ツールのフォルダ(この scripts の1つ上)に次の2つを作ります。
'    資材複合ツール(ブラウザ版).lnk     … Start.vbs
'    資材複合ツール(デスクトップ版).lnk … 梱包資材総合ツール.exe(あれば)
'  指す先は**いまのフォルダの場所**です。フォルダを移したら、もう一度押してください。
'  何度押しても作り直すだけです。
'
'  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
'  WSH reads a .vbs with the system ANSI code page (932 on Japanese Windows).
'  Messages use WScript.Echo: a dialog when double-clicked, text under cscript.
' ===================================================================
Option Explicit

Const APP_NAME = "資材複合ツール"
Const EXE_NAME = "梱包資材総合ツール.exe"

Dim shell, fso, here, made, missing
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
made = ""
missing = ""

MakeLink APP_NAME & "(ブラウザ版).lnk", "Start.vbs", False
MakeLink APP_NAME & "(デスクトップ版).lnk", EXE_NAME, True

Dim msg
If made <> "" Then
    msg = "ショートカットを作りました(場所: " & here & "):" & made
Else
    msg = "ショートカットは1つも作れませんでした(場所: " & here & ")。"
End If
If missing <> "" Then
    msg = msg & vbCrLf & vbCrLf & "次は見つからないので作っていません:" & missing
End If
WScript.Echo msg

Sub MakeLink(linkName, targetName, useIcon)
    Dim target, lnk
    target = fso.BuildPath(here, targetName)
    If Not fso.FileExists(target) Then
        missing = missing & vbCrLf & "  " & targetName
        Exit Sub
    End If
    Set lnk = shell.CreateShortcut(fso.BuildPath(here, linkName))
    lnk.TargetPath = target
    lnk.WorkingDirectory = here
    lnk.Description = APP_NAME & "を起動する"
    If useIcon Then lnk.IconLocation = target & ",0"
    lnk.Save
    made = made & vbCrLf & "  " & linkName & " → " & targetName
End Sub
