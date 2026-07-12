package main

import (
	"bytes"
	"crypto/aes"
	"crypto/md5"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/wdvxdr1123/go-silk"
)

func SaveBase64Image(base64Data string) (string, string, error) {
	rawContents := base64Data
	if strings.HasPrefix(base64Data, "base64://") {
		rawContents = strings.TrimPrefix(base64Data, "base64://")
	} else if idx := strings.Index(base64Data, ","); idx != -1 {
		rawContents = base64Data[idx+1:]
	}

	data, err := base64.StdEncoding.DecodeString(rawContents)
	if err != nil {
		return "", "", fmt.Errorf("base64 decode failed: %v", err)
	}
	salt := []byte(fmt.Sprintf("\n#md5_salt_%d_%d#", time.Now().UnixNano(), rand.Intn(10000)))
	data = append(data, salt...)

	r := rand.New(rand.NewSource(time.Now().UnixNano()))
	randomNumber := r.Intn(1000) // 生成 0-999 的随机数
	timestamp := time.Now().Unix()
	fileName := fmt.Sprintf("%d_%d.%s", randomNumber, timestamp, DetectFileFormat(data))
	targetPath := config.ImagePath + fileName
	dir := filepath.Dir(targetPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return "", "", fmt.Errorf("create directory failed: %v", err)
	}

	err = os.WriteFile(targetPath, data, 0644)
	if err != nil {
		return "", "", fmt.Errorf("write file failed: %v", err)
	}

	md5Str, err := GetFileMD5(targetPath)
	if err != nil {
		return "", "", fmt.Errorf("get file md5 failed: %v", err)
	}

	return targetPath, md5Str, nil
}

// SaveVoiceFile 解码base64音频数据并保存为文件（不追加salt，保持二进制完整性）
// 返回原始字节、文件路径、错误
func SaveVoiceFile(base64Data string) ([]byte, string, error) {
	rawContents := base64Data
	if strings.HasPrefix(base64Data, "base64://") {
		rawContents = strings.TrimPrefix(base64Data, "base64://")
	} else if idx := strings.Index(base64Data, ","); idx != -1 {
		rawContents = base64Data[idx+1:]
	}

	data, err := base64.StdEncoding.DecodeString(rawContents)
	if err != nil {
		return nil, "", fmt.Errorf("base64 decode failed: %v", err)
	}

	r := rand.New(rand.NewSource(time.Now().UnixNano()))
	randomNumber := r.Intn(1000)
	timestamp := time.Now().Unix()
	ext := DetectFileFormat(data)
	fileName := fmt.Sprintf("%d_%d.%s", randomNumber, timestamp, ext)
	targetPath := config.ImagePath + fileName
	dir := filepath.Dir(targetPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return nil, "", fmt.Errorf("create directory failed: %v", err)
	}

	err = os.WriteFile(targetPath, data, 0666)
	if err != nil {
		return nil, "", fmt.Errorf("write file failed: %v", err)
	}
	os.Chmod(targetPath, 0666)

	return data, targetPath, nil
}

// SaveBase64File 解码 base64 数据并以指定扩展名保存文件，返回文件路径和 MD5
func SaveBase64File(base64Data string, ext string) (string, string, error) {
	rawContents := base64Data
	if strings.HasPrefix(base64Data, "base64://") {
		rawContents = strings.TrimPrefix(base64Data, "base64://")
	} else if idx := strings.Index(base64Data, ","); idx != -1 {
		rawContents = base64Data[idx+1:]
	}

	data, err := base64.StdEncoding.DecodeString(rawContents)
	if err != nil {
		return "", "", fmt.Errorf("base64 decode failed: %v", err)
	}

	// 如果没有传入扩展名，尝试自动检测
	if ext == "" {
		ext = DetectFileFormat(data)
		if ext == "unknown" {
			// fallback: 用 MIME 类型推断
			mimeType := http.DetectContentType(data)
			ext = mimeToExt(mimeType)
		}
	}

	r := rand.New(rand.NewSource(time.Now().UnixNano()))
	randomNumber := r.Intn(1000)
	timestamp := time.Now().Unix()
	fileName := fmt.Sprintf("%d_%d.%s", randomNumber, timestamp, ext)
	targetPath := config.ImagePath + fileName
	dir := filepath.Dir(targetPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return "", "", fmt.Errorf("create directory failed: %v", err)
	}

	err = os.WriteFile(targetPath, data, 0666)
	if err != nil {
		return "", "", fmt.Errorf("write file failed: %v", err)
	}
	os.Chmod(targetPath, 0666)

	md5Str, err := GetFileMD5(targetPath)
	if err != nil {
		return "", "", fmt.Errorf("get file md5 failed: %v", err)
	}

	return targetPath, md5Str, nil
}

// mimeToExt 将 MIME 类型转换为文件扩展名
func mimeToExt(mimeType string) string {
	if idx := strings.Index(mimeType, ";"); idx != -1 {
		mimeType = strings.TrimSpace(mimeType[:idx])
	}
	switch mimeType {
	case "text/plain":
		return "txt"
	case "text/html":
		return "html"
	case "text/xml", "application/xml":
		return "xml"
	case "application/json":
		return "json"
	case "application/pdf":
		return "pdf"
	case "application/zip":
		return "zip"
	case "application/gzip":
		return "gz"
	case "image/jpeg":
		return "jpg"
	case "image/png":
		return "png"
	case "image/gif":
		return "gif"
	case "image/webp":
		return "webp"
	case "video/mp4":
		return "mp4"
	case "audio/mpeg":
		return "mp3"
	default:
		return "bin"
	}
}

// ConvertToSilk 将音频数据(任意格式)通过ffmpeg转为PCM，再编码为SILK格式
// 微信要求格式: \x02#!SILK_V3 开头 (tencent silk)
// 如果输入已经是该格式，则直接返回
// 返回: silkData, 时长(毫秒), error
func ConvertToSilk(audioData []byte) ([]byte, int32, error) {
	// 已经是tencent SILK格式 (\x02#!SILK_V3)，直接返回，时长未知设为0
	if len(audioData) > 10 && audioData[0] == 0x02 && bytes.HasPrefix(audioData[1:], []byte("#!SILK_V3")) {
		return audioData, 0, nil
	}

	// 先用ffmpeg将输入音频转为PCM (s16le, 16000Hz, mono)
	cmd := exec.Command("ffmpeg",
		"-i", "pipe:0",
		"-f", "s16le",
		"-ar", "16000",
		"-ac", "1",
		"pipe:1",
	)
	cmd.Stdin = bytes.NewReader(audioData)

	var out bytes.Buffer
	cmd.Stdout = &out
	var stderr bytes.Buffer
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		return nil, 0, fmt.Errorf("ffmpeg to pcm error: %v, details: %s", err, stderr.String())
	}

	pcmBytes := out.Bytes()
	// 时长(ms) = pcm字节数 * 1000 / (采样率 * 通道数 * 每样本字节数)
	durationMs := int32(int64(len(pcmBytes)) * 1000 / (16000 * 2))

	// 尝试使用外部silk-encoder（和微信兼容性更好）
	silkData, err := encodeSilkExternal(pcmBytes)
	if err != nil {
		// fallback: 使用go-silk库
		silkData, err = silk.EncodePcmBuffToSilk(pcmBytes, 16000, 16000, true)
		if err != nil {
			return nil, 0, fmt.Errorf("encode silk error: %v", err)
		}
	}

	return silkData, durationMs, nil
}

// encodeSilkExternal 使用外部pilk(Python)工具编码pcm->silk(和微信兼容)
func encodeSilkExternal(pcmBytes []byte) ([]byte, error) {
	tmpPcm, err := os.CreateTemp("", "voice_*.pcm")
	if err != nil {
		return nil, err
	}
	defer os.Remove(tmpPcm.Name())

	if _, err := tmpPcm.Write(pcmBytes); err != nil {
		tmpPcm.Close()
		return nil, err
	}
	tmpPcm.Close()

	tmpSilk := tmpPcm.Name() + ".silk"
	defer os.Remove(tmpSilk)

	pyScript := fmt.Sprintf(`import pilk; pilk.encode("%s", "%s", pcm_rate=16000, tencent=True)`, tmpPcm.Name(), tmpSilk)
	cmd := exec.Command("python3", "-c", pyScript)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("pilk encode failed: %v, %s", err, stderr.String())
	}
	return os.ReadFile(tmpSilk)
}

// GetVideoDuration 使用ffprobe获取视频时长（秒）
func GetVideoDuration(filePath string) (int32, error) {
	cmd := exec.Command("ffprobe",
		"-v", "error",
		"-show_entries", "format=duration",
		"-of", "default=noprint_wrappers=1:nokey=1",
		filePath,
	)
	var out bytes.Buffer
	cmd.Stdout = &out
	if err := cmd.Run(); err != nil {
		return 0, fmt.Errorf("ffprobe error: %v", err)
	}

	durationStr := strings.TrimSpace(out.String())
	durationFloat, err := strconv.ParseFloat(durationStr, 64)
	if err != nil {
		return 0, fmt.Errorf("parse duration failed: %v", err)
	}

	return int32(durationFloat), nil
}

func GetFileMD5(filePath string) (string, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return "", err
	}
	defer file.Close()

	hash := md5.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// FileToBase64 读取文件并返回 base64 编码字符串
func FileToBase64(filePath string) (string, error) {
	data, err := os.ReadFile(filePath)
	if err != nil {
		return "", fmt.Errorf("read file failed: %v", err)
	}
	return base64.StdEncoding.EncodeToString(data), nil
}

func SaveAudioFile(silkBytes []byte) (path string, err error) {
	mp3Bytes, err := SilkToMp3(silkBytes)
	if err != nil {
		return "", err
	}

	exePath, err := os.Executable()
	if err != nil {
		return "", err
	}

	r := rand.New(rand.NewSource(time.Now().UnixNano()))
	randomNumber := r.Intn(1000)
	timestamp := time.Now().Unix()
	fileName := fmt.Sprintf("%d_%d.mp3", randomNumber, timestamp)
	targetPath := filepath.Dir(exePath) + "/audio/" + fileName
	err = os.WriteFile(targetPath, mp3Bytes, 0644)
	if err != nil {
		return "", err
	}

	return targetPath, nil
}

func SilkToMp3(silkBytes []byte) ([]byte, error) {
	var pcm, err = silk.DecodeSilkBuffToPcm(silkBytes, 16000)
	if err != nil {
		return nil, err
	}

	cmd := exec.Command("ffmpeg",
		"-f", "s16le",
		"-ar", "16000",
		"-ac", "1",
		"-i", "pipe:0",
		"-codec:a", "libmp3lame",
		"-b:a", "192k",
		"-f", "mp3",
		"pipe:1",
	)
	cmd.Stdin = bytes.NewReader(pcm)

	var out bytes.Buffer
	cmd.Stdout = &out
	var stderr bytes.Buffer
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("ffmpeg error: %v, details: %s", err, stderr.String())
	}

	return out.Bytes(), nil
}

// GetFilePath 解密 CDN 数据并落盘。
//   - extHint: 消息 XML 里的 fileext（如 "txt"），优先级最高——有就直接用它当扩展名，
//     不再靠 magic bytes 猜（文本文件没有 magic，猜不出来）。
//   - totalLen: 明文真实长度（消息里的 totallen）。>0 时按它截断，去掉 AES 补齐的尾部字节。
func GetFilePath(data []byte, key []byte, extHint string, totalLen int) (string, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}
	// 只解密对齐部分，丢弃末尾不足一个块的残余（调用方通常已补齐/截齐）。
	bs := block.BlockSize()
	aligned := len(data) - len(data)%bs
	if aligned == 0 {
		return "", fmt.Errorf("invalid encrypted data length: %d, block_size: %d", len(data), bs)
	}

	decrypted := make([]byte, aligned)
	for i := 0; i < aligned; i += bs {
		block.Decrypt(decrypted[i:i+bs], data[i:i+bs])
	}

	// 按真实长度截断，去掉补齐的尾部字节
	if totalLen > 0 && totalLen <= len(decrypted) {
		decrypted = decrypted[:totalLen]
	}

	// 扩展名：优先用消息里的 fileext，其次靠 magic 检测，都没有则存为 bin（不丢数据）
	ext := strings.TrimPrefix(strings.ToLower(extHint), ".")
	if ext == "" {
		ext = DetectFileFormat(decrypted)
		if ext == "unknown" {
			ext = "bin"
		}
	}

	return SaveFileToFile(ext, decrypted)
}

// DetectFileFormat 检测文件格式，返回扩展名
func DetectFileFormat(data []byte) string {
	if len(data) < 8 {
		return "unknown"
	}

	switch {
	// 视频格式
	case bytes.HasPrefix(data, []byte{0x00, 0x00, 0x00}): // MP4/MOV 通常以 ftyp 开头，后面是具体类型
		if len(data) > 4 {
			switch string(data[4:8]) {
			case "ftyp", "moov", "mdat", "wide", "free":
				return "mp4"
			case "isom", "mp41", "mp42", "M4V ", "M4A ", "M4P ":
				return "mp4"
			}
		}
	case bytes.HasPrefix(data, []byte("FLV\x01")): // FLV
		return "flv"
	case bytes.HasPrefix(data, []byte{0x30, 0x26, 0xB2, 0x75, 0x8E, 0x66, 0xCF, 0x11}): // ASF/WMV/WMA
		if len(data) > 8 && bytes.HasPrefix(data[8:], []byte{0xA6, 0xD9, 0x00, 0xAA, 0x00, 0x62, 0xCE, 0x6C}) {
			return "wmv"
		}

	// 图片格式
	case bytes.HasPrefix(data, []byte{0xFF, 0xD8, 0xFF}):
		return "jpg"
	case bytes.HasPrefix(data, []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}):
		return "png"
	case bytes.HasPrefix(data, []byte("GIF87a")) || bytes.HasPrefix(data, []byte("GIF89a")):
		return "gif"
	case bytes.HasPrefix(data, []byte{0x42, 0x4D}):
		return "bmp"
	case bytes.HasPrefix(data, []byte("RIFF")) && len(data) > 8 && bytes.HasPrefix(data[8:], []byte("WEBP")):
		return "webp"

	// 文档格式
	case bytes.HasPrefix(data, []byte("%PDF")):
		return "pdf"

	// Office 2007+ 格式 (docx, xlsx, pptx 都是 ZIP 格式)
	case bytes.HasPrefix(data, []byte{0x50, 0x4B, 0x03, 0x04}):
		return detectOfficeFormat(data)

	// Office 97-2003 格式 (OLE2 格式)
	case bytes.HasPrefix(data, []byte{0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1}):
		return detectLegacyOfficeFormat(data)

	// 压缩文件
	case bytes.HasPrefix(data, []byte("Rar!\x1a\x07")):
		return "rar"
	case bytes.HasPrefix(data, []byte("7z\xBC\xAF\x27\x1C")):
		return "7z"

	// 音频格式
	case data[0] == 0x02 && bytes.HasPrefix(data[1:], []byte("#!SILK")):
		return "silk"
	case bytes.HasPrefix(data, []byte("#!SILK")):
		return "silk"
	case bytes.HasPrefix(data, []byte("RIFF")) && len(data) > 8 && bytes.HasPrefix(data[8:], []byte("WAVE")):
		return "wav"
	case bytes.HasPrefix(data, []byte{0xFF, 0xFB}) || bytes.HasPrefix(data, []byte{0xFF, 0xF3}) || bytes.HasPrefix(data, []byte{0xFF, 0xF2}):
		return "mp3"
	case bytes.HasPrefix(data, []byte("ID3")):
		return "mp3"
	case bytes.HasPrefix(data, []byte("OggS")):
		return "ogg"
	case bytes.HasPrefix(data, []byte("fLaC")):
		return "flac"

	default:
		return "unknown"
	}

	return "unknown"
}

// detectOfficeFormat 检测 Office 2007+ 文件具体类型
func detectOfficeFormat(data []byte) string {
	// 查找 ZIP 内的特定文件来区分类型
	if bytes.Contains(data, []byte("[Content_Types].xml")) {
		if bytes.Contains(data, []byte("word/")) {
			return "docx"
		}
		if bytes.Contains(data, []byte("xl/")) {
			return "xlsx"
		}
		if bytes.Contains(data, []byte("ppt/")) {
			return "pptx"
		}
	}
	// 普通 ZIP 文件
	return "zip"
}

// detectLegacyOfficeFormat 检测 Office 97-2003 文件具体类型
func detectLegacyOfficeFormat(data []byte) string {
	// 通过文件内容特征判断
	if bytes.Contains(data, []byte("Word.Document")) {
		return "doc"
	}
	if bytes.Contains(data, []byte("Excel.Sheet")) {
		return "xls"
	}
	if bytes.Contains(data, []byte("PowerPoint.Show")) {
		return "ppt"
	}
	return "ole"
}

// SaveFileToFile 通用文件保存函数
func SaveFileToFile(ext string, data []byte) (string, error) {
	exePath, err := os.Executable()
	if err != nil {
		return "", err
	}

	r := rand.New(rand.NewSource(time.Now().UnixNano()))
	randomNumber := r.Intn(1000)
	timestamp := time.Now().Unix()

	// 根据文件类型选择保存目录
	dir := "file"
	if ext == "jpg" || ext == "png" || ext == "gif" || ext == "bmp" || ext == "webp" {
		dir = "image"
	}

	fileName := fmt.Sprintf("%d_%d.%s", randomNumber, timestamp, ext)
	targetPath := filepath.Dir(exePath) + "/" + dir + "/" + fileName

	// 确保目录存在
	if err := os.MkdirAll(filepath.Dir(targetPath), 0755); err != nil {
		return "", err
	}

	err = os.WriteFile(targetPath, data, 0644)
	if err != nil {
		return "", err
	}

	return targetPath, nil
}

func SaveImageToFile(ext string, data []byte) (string, error) {
	exePath, err := os.Executable()
	if err != nil {
		return "", err
	}

	r := rand.New(rand.NewSource(time.Now().UnixNano()))
	randomNumber := r.Intn(1000)
	timestamp := time.Now().Unix()
	fileName := fmt.Sprintf("%d_%d.%s", randomNumber, timestamp, ext)
	targetPath := filepath.Dir(exePath) + "/image/" + fileName
	err = os.WriteFile(targetPath, data, 0644)
	if err != nil {
		return "", err
	}

	return targetPath, nil
}

func GetWeChatPID() (int, error) {
	cmd := exec.Command("pgrep", "-x", "WeChat")
	output, err := cmd.Output()
	if err != nil {
		return 0, fmt.Errorf("未发现正在运行的微信进程")
	}

	return strconv.Atoi(strings.TrimSpace(string(output)))
}

func DownloadFile(urlStr string) ([]byte, error) {
	if urlStr == "" {
		return nil, errors.New("url is empty")
	}

	// 解析 URL 以判断协议
	parsedURL, err := url.Parse(urlStr)
	if err != nil {
		return nil, errors.New("invalid URL format: " + err.Error())
	}

	// 处理 file:// 协议
	if parsedURL.Scheme == "file" {
		// 去除 "file://" 前缀，得到本地文件路径
		filePath := strings.TrimPrefix(urlStr, "file://")
		// 对于 Windows 路径可能需要额外处理，但你的路径是 macOS/Linux 格式
		data, err := os.ReadFile(filePath)
		if err != nil {
			return nil, errors.New("failed to read local file: " + err.Error())
		}
		return data, nil
	}

	client := &http.Client{}
	resp, err := client.Get(urlStr)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("failed to download file: " + resp.Status)
	}

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	return data, nil
}

// DetectAndSaveImage 自动检测图片格式并保存到本地
func DetectAndSaveImage(data []byte) (string, error) {
	// 先检测图片格式
	ext := DetectFileFormat(data)
	if ext == "unknown" {
		return "", fmt.Errorf("无法识别的图片格式")
	}

	// 调用保存函数
	return SaveImageToFile(ext, data)
}

// MonitorProcess 监控指定 PID 的进程是否退出
// 如果进程退出，清理 Frida 资源并等待微信重新启动后重新 attach
func MonitorProcess(pid int) {
	Info("开始监控微信进程", "PID", pid)
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()

		for range ticker.C {
			proc, err := os.FindProcess(pid)
			if err != nil {
				Info("微信进程已退出，清理 Frida 资源，等待微信重新启动...")
				cleanAndReattach()
				return
			}

			// 检查进程是否存活
			err = proc.Signal(syscall.Signal(0))
			if err != nil {
				Info("微信进程已退出，清理 Frida 资源，等待微信重新启动...")
				cleanAndReattach()
				return
			}
		}
	}()
}

func cleanAndReattach() {
	if fridaScript != nil {
		fridaScript.Clean()
		Info("Frida 脚本资源已清理")
	}
	if session != nil {
		session.Clean()
		Info("Frida 会话资源已清理")
	}

	Info("等待微信重新启动...")
	// 重新等待微信进程并 attach
	attachWechat()
}

// HexDump formats data like:
// 0000000C12334C00  0A 40 0A 01 00 10 C6 BC  90 B9 08 1A 10 6D 36 34  .@....Ƽ.....m64
func HexDump(data []byte, baseAddr uint64) string {
	var sb strings.Builder
	for i := 0; i < len(data); i += 16 {
		// Address
		sb.WriteString(fmt.Sprintf("%016X  ", baseAddr+uint64(i)))

		// Hex bytes
		for j := 0; j < 16; j++ {
			if j == 8 {
				sb.WriteByte(' ')
			}
			if i+j < len(data) {
				sb.WriteString(fmt.Sprintf("%02X ", data[i+j]))
			} else {
				sb.WriteString("   ")
			}
		}

		// ASCII
		sb.WriteByte(' ')
		for j := 0; j < 16; j++ {
			if i+j < len(data) {
				b := data[i+j]
				if b >= 0x20 && b <= 0x7E {
					sb.WriteByte(b)
				} else {
					sb.WriteByte('.')
				}
			}
		}
		sb.WriteByte('\n')
	}
	return sb.String()
}
