package main

import (
	"encoding/json"
	"time"
)

func cleanExpiredDownloads() {
	ticker := time.NewTicker(1 * time.Hour)
	for range ticker.C {
		now := time.Now().UnixMilli()
		userID2FileMsgMap.Range(func(key, value any) bool {
			req := value.(*DownloadRequest)
			req.mu.Lock()
			expired := now-req.LastAppendTime > 24*60*60*1000
			req.mu.Unlock()
			if expired {
				userID2FileMsgMap.Delete(key)
			}
			return true
		})
	}
}

func Download(rawMsg []byte) error {
	downloadReq := new(DownloadRequest)
	err := json.Unmarshal(rawMsg, downloadReq)
	if err != nil {
		Error("JSON解析失败", "err", err)
		return err
	}

	Info("下载文件", "file_id", downloadReq.FileID, "media_len", len(downloadReq.Media), "cdn_url", downloadReq.CDNURL)

	// LoadOrStore 保证同一 CDNURL 只建一份，消除 Load+Store 之间的竞态。
	// 首个分片直接作为占位存入，后续分片走 append 分支。
	actual, loaded := userID2FileMsgMap.LoadOrStore(downloadReq.CDNURL, downloadReq)
	req := actual.(*DownloadRequest)

	req.mu.Lock()
	defer req.mu.Unlock()

	if !loaded {
		// 本次就是首个分片（downloadReq 已被存入），只更新时间戳
		req.LastAppendTime = time.Now().UnixMilli()
		return nil
	}

	if req.FilePath != "" {
		return nil
	}
	if time.Now().UnixMilli()-req.LastAppendTime > 10000000 {
		// 距上次追加过久，视为新文件，重置数据
		req.Media = append(req.Media[:0], downloadReq.Media...)
	} else {
		req.Media = append(req.Media, downloadReq.Media...)
	}
	req.LastAppendTime = time.Now().UnixMilli()

	return nil
}
