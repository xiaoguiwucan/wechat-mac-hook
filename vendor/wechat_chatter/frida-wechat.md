触发 STNManager__MMStartTask （ida搜 [MMStartTask]()）
图片hook: startUploadMedia （ida搜）, OnUploadCompleted （ce查）
startUploadMedia 上传图片，OnUploadCompleted 上传图片完成后的回调
加密后的图片在 _OnRecvFileData

var downloadFileAddr  //  c2c_download 文件和缩略图
var downloadImagAddr // image_download 高清图
var downloadVideoAddr // hdvideo_streaming OnRecvedData

WeChatExt中会有导致idapro出问题的代码
