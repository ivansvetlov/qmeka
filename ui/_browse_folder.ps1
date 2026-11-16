Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "Qmeka — working folder"
$dialog.ShowNewFolderButton = $true
$form = New-Object System.Windows.Forms.Form
$form.TopMost = $true
$form.WindowState = "Minimized"
$form.ShowInTaskbar = $false
$null = $form.Show()
$form.Hide()
$result = $dialog.ShowDialog($form)
$form.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.SelectedPath)
}
