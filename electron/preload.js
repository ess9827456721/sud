const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  notifyError:  (msg)        => ipcRenderer.send('notify-error', msg),
  notifyInfo:   (title, msg) => ipcRenderer.send('notify-info', title, msg),
  openExternal: (url)        => ipcRenderer.send('open-external', url),
  getVersion:   ()           => ipcRenderer.invoke('get-app-version'),
  onFlaskError: (cb)         => ipcRenderer.on('flask-error', (_, msg) => cb(msg)),
});
