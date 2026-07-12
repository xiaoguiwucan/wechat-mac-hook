package main

import (
	"fmt"

	"google.golang.org/protobuf/proto"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
)

// ParseSendMsgResponse 解析发送消息响应，提取 BaseResponse 中的错误码
// 不同消息类型的响应格式不同：
//   - text/video/image/file/reply: BaseResponse 在 field 1 → SendMsgResponse
//   - voice: BaseResponse 在 field 10 → SendVoiceMsgResponse
//
// 返回 (ret, errMsg, error)
// 如果两种格式都无法提取到有效的 BaseResponse，返回 ret=0 视为成功
func ParseSendMsgResponse(data []byte) (int32, string, error) {
	// 尝试格式1: BaseResponse 在 field 1 (text/video/image/file/reply)
	resp1 := &wxproto.SendMsgResponse{}
	if err := proto.Unmarshal(data, resp1); err == nil {
		if resp1.BaseResponse != nil && resp1.BaseResponse.Ret != nil {
			errMsg := ""
			if resp1.BaseResponse.ErrMsg != nil {
				errMsg = resp1.BaseResponse.ErrMsg.GetMsg()
			}
			return *resp1.BaseResponse.Ret, errMsg, nil
		}
	}

	// 尝试格式2: BaseResponse 在 field 10 (voice)
	resp2 := &wxproto.SendVoiceMsgResponse{}
	if err := proto.Unmarshal(data, resp2); err == nil {
		if resp2.BaseResponse != nil && resp2.BaseResponse.Ret != nil {
			errMsg := ""
			if resp2.BaseResponse.ErrMsg != nil {
				errMsg = resp2.BaseResponse.ErrMsg.GetMsg()
			}
			return *resp2.BaseResponse.Ret, errMsg, nil
		}
	}

	// 两种格式都未提取到 BaseResponse，视为成功
	return 0, "", fmt.Errorf("unable to extract BaseResponse from response")
}
