package main

import (
	"fmt"
	"testing"
)

func TestFileToBase64(t *testing.T) {
	// 测试文件不存在
	f, _ := FileToBase64("/Users/yincong/Desktop/2_base64.txt")
	fmt.Println(f)
}
