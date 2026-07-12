# 全新思路
https://bbs.kanxue.com/thread-286611.htm    

## 步骤
1. 下载mars https://github.com/Tencent/mars/tree/master?tab=readme-ov-file#mars_cn
 ```
 cd mars
 python3 build_osx.py
 cd  cmake_build/OSX/Darwin.out
 ar -x libxlog.a
for f in *.o; do
    /Applications/IDA\ Professional\ 9.1.app/Contents/MacOS/tools/flair/pmacho "$f"
done

/Applications/IDA\ Professional\ 9.1.app/Contents/MacOS/tools/flair/sigmake *pat a.sig 
 ```
使用flair    
```
/Applications/IDA\ Professional\ 9.1.app/Contents/MacOS/tools/flair/pmacho libxlog.a libxlog.pat


```
