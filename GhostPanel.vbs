' GhostPanel portable: arranca el panel sin ninguna ventana de consola.
'
' No lleva rutas fijas a proposito: se calcula todo a partir de donde esta
' este propio archivo, asi que da igual la letra que Windows asigne al USB.
Option Explicit

Dim fso, shell, base, exe, app

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
exe = base & "\python\pythonw.exe"
app = base & "\ghostpanel.pyw"

If Not fso.FileExists(exe) Then
    MsgBox "Falta el Python portable:" & vbCrLf & exe, 16, "GhostPanel"
    WScript.Quit 1
End If

If Not fso.FileExists(app) Then
    MsgBox "Falta el script:" & vbCrLf & app, 16, "GhostPanel"
    WScript.Quit 1
End If

shell.CurrentDirectory = base

' pythonw.exe no tiene consola, y el 0 evita ademas cualquier parpadeo de
' ventana al lanzarlo. El False es para no esperar a que el panel se cierre.
shell.Run """" & exe & """ """ & app & """", 0, False
