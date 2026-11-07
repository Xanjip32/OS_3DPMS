' Starts OS_3DPMS with no console window. Logs go to dashboard.log next to this file.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
args = ""
If WScript.Arguments.Count > 0 Then
  If WScript.Arguments(0) = "--open-setup" Then args = " --open-setup"
End If
script = " """ & dir & "\run.py""" & args
pythonw = dir & "\runtime\pythonw.exe"
If fso.FileExists(pythonw) Then
  sh.Run """" & pythonw & """" & script, 0, False
  WScript.Quit
End If
On Error Resume Next
sh.Run "pyw -3" & script, 0, False
If Err.Number <> 0 Then
  Err.Clear
  sh.Run "pythonw" & script, 0, False
End If
