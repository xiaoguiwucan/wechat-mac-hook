# 克隆源码
git clone https://github.com/Tyilo/insert_dylib

# 进入目录
cd insert_dylib

# 使用 Xcode 命令行工具进行编译
xcodebuild

# 将生成的二进制文件移动到系统路径，方便全局调用
cp build/Release/insert_dylib /usr/local/bin/           
wget https://github.com/frida/frida/releases/download/17.5.2/frida-gadget-17.5.2-macos-universal.dylib.xz       
xz -d frida-gadget-17.5.2-macos-universal.dylib.xz      
cp frida-gadget-17.5.2-macos-universal.dylib /Applications/WeChat.app/Contents/Frameworks/FridaGadget.dylib        
chmod +x /Applications/WeChat.app/Contents/Frameworks/FridaGadget.dylib
cd /Applications/WeChat.app/Contents/MacOS/            
/usr/local/bin/insert_dylib --inplace --strip-codesig "@executable_path/../Frameworks/FridaGadget.dylib" WeChat            

# 回到weixin-macos目录下
./frida-gadget/sign.sh           
cp frida-gadget/FridaGadget.config /Applications/WeChat.app/Contents/Frameworks/        

# 如果遇到weixin启动没反应执行以下命令，一切正常则可以跳过这条命令
sudo codesign --force --deep --sign - /Applications/WeChat.app

frida -H 127.0.0.1:27042 -n Gadget -l ./frida/script.js   
