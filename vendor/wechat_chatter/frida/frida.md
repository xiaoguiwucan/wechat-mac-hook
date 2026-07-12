frida -f /Applications/WeChat.app/Contents/MacOS/WeChat -l script.js        
frida-trace -p 57961 -i '*encrypt*' -i "*Encrypt*"  -x '*objc_msgSend_noarg*' -x '*objc_msgSend_debug*' -x '*objc_msgSend*' -x "*_HIDisableSuddenTerminationForSendEvent*" -x "*_HIEnableSuddenTerminationForSendEvent*" -x "*SendEventToEventTarget*" -x "*s10RTCUtility10XPCMessageV4dictAA16RTCXPCDictionaryVvg*" -x "*MTLMessageContextEnd*" -x "*ictAA16RTCXPCDictionaryVvg*" -x "*_MTLMessageContextBegin_*" -x "*CFMachMessageCheckForAndDestroyUnsentMessag*" -x "*SLEventCopyAuthenticationMessage*" -x "*SendTextInputEvent_WithCompletionHandler*" -x '*mach_msg_send*' -x "*dispatch_mach_send_with_result_and_async_reply_4libxpc*" -x "*dispatch_mach_send_with_result_and_async_reply_4libxpc*" -x "*dispatch_mach_send_with_result*" --decorate --ui-port 60000     
frida-trace -p 进程号 -i "*Message*" --decorate 比较好用       
frida  -p 79464 -l ./script.js     

# 怎么使用succ.js
manualTrigger(0x20000095, "wxid_", "哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈")