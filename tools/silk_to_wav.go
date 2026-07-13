package main

import (
    "encoding/binary"
    "fmt"
    "os"

    silk "github.com/wdvxdr1123/go-silk"
)

func writeWav(path string, pcm []byte, sampleRate uint32) error {
    f, err := os.Create(path)
    if err != nil { return err }
    defer f.Close()
    channels := uint16(1)
    bitsPerSample := uint16(16)
    byteRate := sampleRate * uint32(channels) * uint32(bitsPerSample/8)
    blockAlign := channels * bitsPerSample / 8
    dataLen := uint32(len(pcm))
    if _, err = f.Write([]byte("RIFF")); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, uint32(36)+dataLen); err != nil { return err }
    if _, err = f.Write([]byte("WAVEfmt ")); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, uint32(16)); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, uint16(1)); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, channels); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, sampleRate); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, byteRate); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, blockAlign); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, bitsPerSample); err != nil { return err }
    if _, err = f.Write([]byte("data")); err != nil { return err }
    if err = binary.Write(f, binary.LittleEndian, dataLen); err != nil { return err }
    _, err = f.Write(pcm)
    return err
}

func main() {
    if len(os.Args) != 3 {
        fmt.Fprintln(os.Stderr, "usage: silk_to_wav input.silk output.wav")
        os.Exit(2)
    }
    raw, err := os.ReadFile(os.Args[1])
    if err != nil { panic(err) }
    pcm, err := silk.DecodeSilkBuffToPcm(raw, 16000)
    if err != nil { panic(err) }
    if err := writeWav(os.Args[2], pcm, 16000); err != nil { panic(err) }
    durationMs := int64(len(pcm)) * 1000 / (16000 * 2)
    fmt.Printf("ok pcm=%d duration_ms=%d\n", len(pcm), durationMs)
}
