## 如何使用onebot的http接口

### 使用方式和脚本基本一致：        
   1. 编译main.go或者直接下载编译好的onebot二进制文件       
   2. 如果不想关闭SIP直接使用，需要按gadget文档操作wechat的二进制，[文档](https://github.com/yincongcyincong/weixin-macos/tree/main/frida-gadget),需要找到自己的图片位置：./onebot -type=gadget -image_path='/Users/xx/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_xxx/temp/xxx/2026-01/Img/'
   3. 如果关闭了SIP， 直接使用pid即可，./onebot -image_path='/Users/xxx/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_xxx/temp/xxx/2026-01/Img/'
   4. 发送一张图片，如果成功证明已经patch成功，可以正常使用。
   5. 启动onebot服务，默认监听127.0.0.1:58080，可以通过http接口发送消息。
   6. 会把收到的消息通过 http://127.0.0.1:36060/onebot 其他参数可用./onebot -h查看


### 接口信息
私聊是send_private_msg     
群聊是send_group_msg       

```
curl -i -X POST \
   -H "Content-Type:application/json" \
   -d \
'{
  "message" : [{
    "data" : {
      "text" : "🚀successfully delete!"
    },
    "type" : "text"
  },{
    "data" : {
      "file" : "base64://"
    },
    "type" : "image"
  } ,{
    "data" : {
      "file" : "base64://"
    },
    "type" : "video"
  } ],
  "user_id" : "wxid_xxx"
}' \
 'http://127.0.0.1:58080/send_private_msg'
 
返回结果：
{"status":"ok"}

```

## 接入openclaw
```
# 进入插件目录
cd openclaw/extensions
# 克隆仓库
git clone https://github.com/constansino/openclaw_qq.git qq
# 安装依赖并构建
cd ../..
pnpm install && pnpm build
# 安装openclaw命令
npm install -g openclaw@latest
# 根据 https://github.com/constansino/openclaw_qq 把extension放到extensions目录下
openclaw plugins install ./extensions/qq
# 启动openclaw
openclaw gateway run
# 启动onebot
./onebot -wechat_pid=18835 -image_path='/Users/xxx/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_xxx/temp/xxx/2026-01/Img/ -conn_type=websocket
```

