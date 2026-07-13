#import <AppKit/AppKit.h>
#import <CoreGraphics/CoreGraphics.h>
#import <Vision/Vision.h>
#include <dlfcn.h>

typedef CGImageRef (*WindowImageFunction)(CGRect, CGWindowListOption, CGWindowID, CGWindowImageOption);

static CGImageRef CaptureWindow(CGWindowID window) {
  static WindowImageFunction function = NULL;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    void *coreGraphics = dlopen("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", RTLD_LAZY);
    function = (WindowImageFunction)dlsym(coreGraphics, "CGWindowListCreateImage");
  });
  CGImageRef image = function ? function(CGRectNull, kCGWindowListOptionIncludingWindow, window, kCGWindowImageBoundsIgnoreFraming | kCGWindowImageBestResolution) : NULL;
  if (image) return image;

  // A detached helper may not inherit the WindowServer capture attribution used by
  // an interactive terminal. The system capture utility keeps the same target
  // window restriction while avoiding that process-identity difference.
  NSString *path = [NSTemporaryDirectory() stringByAppendingPathComponent:[NSString stringWithFormat:@"wechat2-ocr-%u.png", window]];
  [[NSFileManager defaultManager] removeItemAtPath:path error:nil];
  NSTask *task = [[NSTask alloc] init];
  task.launchPath = @"/usr/sbin/screencapture";
  task.arguments = @[@"-x", @"-l", [NSString stringWithFormat:@"%u", window], path];
  @try {
    [task launch];
    [task waitUntilExit];
  } @catch (NSException *exception) {
    return NULL;
  }
  if (task.terminationStatus != 0) return NULL;
  NSBitmapImageRep *rep = [NSBitmapImageRep imageRepWithContentsOfFile:path];
  [[NSFileManager defaultManager] removeItemAtPath:path error:nil];
  return rep.CGImage ? CGImageRetain(rep.CGImage) : NULL;
}

static CGWindowID FindWindow(pid_t pid) {
  CFArrayRef copied = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
  NSArray<NSDictionary *> *rows = CFBridgingRelease(copied);
  CGWindowID best = kCGNullWindowID;
  CGFloat bestArea = 0;
  for (NSDictionary *row in rows) {
    if ([row[(id)kCGWindowOwnerPID] intValue] != pid || [row[(id)kCGWindowLayer] intValue] != 0) continue;
    CGRect bounds = CGRectZero;
    if (!CGRectMakeWithDictionaryRepresentation((CFDictionaryRef)row[(id)kCGWindowBounds], &bounds)) continue;
    CGFloat area = bounds.size.width * bounds.size.height;
    if (bounds.size.width < 400 || bounds.size.height < 400 || area <= bestArea) continue;
    best = [row[(id)kCGWindowNumber] unsignedIntValue];
    bestArea = area;
  }
  return best;
}

static NSArray<NSDictionary *> *Recognize(CGImageRef image, NSError **error) {
  VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
  request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
  request.recognitionLanguages = @[@"zh-Hans", @"en-US"];
  request.usesLanguageCorrection = NO;
  VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithCGImage:image options:@{}];
  if (![handler performRequests:@[request] error:error]) return @[];
  NSMutableArray<NSDictionary *> *rows = [NSMutableArray array];
  for (VNRecognizedTextObservation *observation in request.results) {
    VNRecognizedText *candidate = [[observation topCandidates:1] firstObject];
    NSString *text = [candidate.string stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if (!text.length) continue;
    CGRect box = observation.boundingBox;
    [rows addObject:@{
      @"text": text,
      @"x": @(box.origin.x),
      @"y": @(1.0 - box.origin.y - box.size.height),
      @"width": @(box.size.width),
      @"height": @(box.size.height),
    }];
  }
  return [rows sortedArrayUsingComparator:^NSComparisonResult(NSDictionary *left, NSDictionary *right) {
    CGFloat ly = [left[@"y"] doubleValue], ry = [right[@"y"] doubleValue];
    if (fabs(ly - ry) > 0.01) return ly < ry ? NSOrderedAscending : NSOrderedDescending;
    CGFloat lx = [left[@"x"] doubleValue], rx = [right[@"x"] doubleValue];
    return lx < rx ? NSOrderedAscending : (lx > rx ? NSOrderedDescending : NSOrderedSame);
  }];
}

static void PrintJSON(NSDictionary *value) {
  NSError *error = nil;
  NSData *data = [NSJSONSerialization dataWithJSONObject:value options:0 error:&error];
  if (!data) {
    fprintf(stderr, "json error: %s\n", error.localizedDescription.UTF8String);
    return;
  }
  fwrite(data.bytes, 1, data.length, stdout);
  fputc('\n', stdout);
  fflush(stdout);
}

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    pid_t pid = 0;
    BOOL watch = NO;
    int intervalMs = 1500;
    for (int i = 1; i < argc; i++) {
      NSString *arg = [NSString stringWithUTF8String:argv[i]];
      if ([arg isEqualToString:@"--pid"] && i + 1 < argc) pid = (pid_t)atoi(argv[++i]);
      else if ([arg isEqualToString:@"--watch"]) watch = YES;
      else if ([arg isEqualToString:@"--interval-ms"] && i + 1 < argc) intervalMs = MAX(500, atoi(argv[++i]));
    }
    if (pid <= 0) {
      fprintf(stderr, "--pid is required\n");
      return 2;
    }
    NSMutableDictionary<NSString *, NSDate *> *visible = [NSMutableDictionary dictionary];
    BOOL baseline = YES;
    do {
      @autoreleasepool {
        CGWindowID window = FindWindow(pid);
        if (window == kCGNullWindowID) {
          fprintf(stderr, "second WeChat window not found for pid=%d\n", pid);
        } else {
          CGImageRef image = CaptureWindow(window);
          if (!image) {
            fprintf(stderr, "second WeChat window capture failed\n");
          } else {
            NSError *error = nil;
            NSArray<NSDictionary *> *rows = Recognize(image, &error);
            CGImageRelease(image);
            if (error) {
              fprintf(stderr, "vision error: %s\n", error.localizedDescription.UTF8String);
            } else if (!watch) {
              PrintJSON(@{ @"type": @"snapshot", @"items": rows });
            } else {
              NSDate *now = [NSDate date];
              NSMutableSet<NSString *> *frame = [NSMutableSet set];
              for (NSDictionary *row in rows) {
                NSString *text = row[@"text"];
                [frame addObject:text];
                if (!baseline && !visible[text]) PrintJSON(@{ @"type": @"candidate", @"text": text, @"x": row[@"x"], @"y": row[@"y"] });
                visible[text] = now;
              }
              for (NSString *text in [visible allKeys]) {
                if (![frame containsObject:text] && [now timeIntervalSinceDate:visible[text]] > 5.0) [visible removeObjectForKey:text];
              }
              baseline = NO;
            }
          }
        }
      }
      if (watch) [NSThread sleepForTimeInterval:intervalMs / 1000.0];
    } while (watch);
  }
  return 0;
}
