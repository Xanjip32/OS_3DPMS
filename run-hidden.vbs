' Starts OS_3DPMS with no console window. Logs go to dashboard.log next to this file.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
On Error Resume Next
sh.Run "pyw -3 """ & dir & "\run.py""", 0, False
If Err.Number <> 0 Then
  Err.Clear
  sh.Run "pythonw """ & dir & "\run.py""", 0, False
End If
