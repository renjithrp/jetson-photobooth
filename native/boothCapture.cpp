// "AF Shooting" sample
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <future>
#include <iostream>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#if !defined(__APPLE__)
  #if defined(USE_EXPERIMENTAL_FS) // for jetson
    #include <experimental/filesystem>
    namespace fs = std::experimental::filesystem;
  #else
    #include <filesystem>
    namespace fs = std::filesystem;
  #endif
#endif

#if defined(__APPLE__) || defined(__linux__)
  #include <unistd.h>
#endif

// macro for multibyte character
#if defined(_WIN32) || defined(_WIN64)
  using CrString = std::wstring;
  #define CRSTR(s) L ## s
  #define CrCout std::wcout
#else
  using CrString = std::string;
  #define CRSTR(s) s
  #define CrCout std::cout
#endif


#include "CrDeviceProperty.h"
#include "CameraRemote_SDK.h"
#include "IDeviceCallback.h"
#include "CrDebugString.h"   // use CrDebugString.cpp

#define PrintError(msg, err) { fprintf(stderr, "Error in %s(%d):" msg ",%s\n", __FUNCTION__, __LINE__, (err ? CrErrorString(err).c_str():"")); }
#define GotoError(msg, err) { PrintError(msg, err); goto Error; }

bool  m_connected = false;
CrString m_modelId;
int64_t  m_device_handle = 0;

std::mutex m_eventPromiseMutex;
std::promise<void>* m_eventPromise = nullptr;
void setEventPromise(std::promise<void>* dp)
{
    std::lock_guard<std::mutex> lock(m_eventPromiseMutex);
    m_eventPromise = dp;
}

std::mutex m_focusPromiseMutex;
std::promise<void>* m_focusPromise = nullptr;
void setFocusPromise(std::promise<void>* dp)
{
    std::lock_guard<std::mutex> lock(m_focusPromiseMutex);
    m_focusPromise = dp;
}

SCRSDK::CrError _getDeviceProperty(int64_t device_handle, uint32_t code, SCRSDK::CrDeviceProperty* devProp)
{
    std::int32_t nprop = 0;
    SCRSDK::CrDeviceProperty* prop_list = nullptr;
    SCRSDK::CrError err = SCRSDK::GetSelectDeviceProperties(device_handle, 1, &code, &prop_list, &nprop);
    if(err) GotoError("", err);
    if(prop_list && nprop >= 1) {
        *devProp = prop_list[0];
    }
Error:
    if(prop_list) SCRSDK::ReleaseDeviceProperties(device_handle, prop_list);
    return err;
}

class DeviceCallback : public SCRSDK::IDeviceCallback
{
public:
    DeviceCallback() {};
    ~DeviceCallback() {};

    void OnConnected(SCRSDK::DeviceConnectionVersioin version)
    {
        CrCout << "Connected to " << m_modelId << "\n";
        m_connected = true;
        std::lock_guard<std::mutex> lock(m_eventPromiseMutex);
        if(m_eventPromise) {
            m_eventPromise->set_value();
            m_eventPromise = nullptr;
        }
    }

    void OnError(CrInt32u error)
    {
        printf("Connection error:%s\n", CrErrorString(error).c_str());
        std::lock_guard<std::mutex> lock(m_eventPromiseMutex);
        if(m_eventPromise) {
            m_eventPromise->set_exception(std::make_exception_ptr(std::runtime_error("error")));
            m_eventPromise = nullptr;
        }
    }

    void OnDisconnected(CrInt32u error)
    {
        CrCout << "Disconnected from " << m_modelId << "\n";
        m_connected = false;
        std::lock_guard<std::mutex> lock(m_eventPromiseMutex);
        if(m_eventPromise) {
            m_eventPromise->set_value();
            m_eventPromise = nullptr;
        }
    }

    void OnCompleteDownload(CrChar* filename, CrInt32u type )
    {
        CrCout << "OnCompleteDownload:" << filename << "\n";
        if(m_eventPromise) {
            m_eventPromise->set_value();
            m_eventPromise = nullptr;
        }
    }

    void OnNotifyContentsTransfer(CrInt32u notify, SCRSDK::CrContentHandle contentHandle, CrChar* filename)
    {
        printf("OnNotifyContentsTransfer notify=0x%x handle=%llu file=%s\n",
            notify, (unsigned long long)contentHandle, filename ? (char*)filename : "(null)");
    }

    void OnNotifyRemoteTransferResult(CrInt32u notify, CrInt32u per, CrChar* filename)
    {
        printf("OnNotifyRemoteTransferResult notify=0x%x per=%u file=%s\n",
            notify, per, filename ? (char*)filename : "(null)");
    }

    void OnNotifyRemoteTransferResult(CrInt32u notify, CrInt32u per, CrInt8u* data, CrInt64u size)
    {
        printf("OnNotifyRemoteTransferResult(data) notify=0x%x per=%u size=%llu\n",
            notify, per, (unsigned long long)size);
    }

    void OnNotifyRemoteTransferContentsListChanged(CrInt32u notify, CrInt32u slotNumber, CrInt32u addSize)
    {
        printf("OnNotifyRemoteTransferContentsListChanged notify=0x%x slot=%u add=%u\n",
            notify, slotNumber, addSize);
    }

    void OnNotifyPostViewImage(CrChar* filename, CrInt32u size)
    {
        printf("OnNotifyPostViewImage file=%s size=%u\n", filename ? (char*)filename : "(null)", size);
    }

    void OnWarning(CrInt32u warning)
    {
        if (warning == SCRSDK::CrWarning_Connect_Reconnecting) {
            CrCout << "Reconnecting to " << m_modelId << "\n";
            return;
        }
        printf("OnWarning: code=0x%x %s\n", warning, CrErrorString(warning).c_str());
    }

    void OnWarningExt(CrInt32u warning, CrInt32 param1, CrInt32 param2, CrInt32 param3) {}
    void OnLvPropertyChanged() {}
    void OnLvPropertyChangedCodes(CrInt32u num, CrInt32u* codes) {}
    void OnPropertyChanged() {}
    void OnPropertyChangedCodes(CrInt32u num, CrInt32u* codes)
    {
        //std::cout << "OnPropertyChangedCodes:\n";
        for(uint32_t i = 0; i < num; ++i) {
            std::lock_guard<std::mutex> lock(m_focusPromiseMutex);
            if(codes[i] == SCRSDK::CrDeviceProperty_FocusIndication) {
                SCRSDK::CrDeviceProperty devProp;
                SCRSDK::CrError err = _getDeviceProperty(m_device_handle, codes[i], &devProp);
                if(err) continue;
                printf("FocusIndication=0x%x\n", (int)devProp.GetCurrentValue());
                switch(devProp.GetCurrentValue()) {
                case SCRSDK::CrFocusIndicator_Focused_AF_S:
                case SCRSDK::CrFocusIndicator_Focused_AF_C:
                    if(m_focusPromise) {
                        m_focusPromise->set_value();
                        m_focusPromise = nullptr;
                    }
                    break;
                case SCRSDK::CrFocusIndicator_Unlocked:
                case SCRSDK::CrFocusIndicator_NotFocused_AF_S:
                case SCRSDK::CrFocusIndicator_NotFocused_AF_C:
                case SCRSDK::CrFocusIndicator_TrackingSubject_AF_C:
                case SCRSDK::CrFocusIndicator_Unpause:
                case SCRSDK::CrFocusIndicator_Pause:
                default:
                    break;
                }
            }
/* for debug
            std::string name = CrDevicePropertyString((SCRSDK::CrDevicePropertyCode)codes[i]);

            SCRSDK::CrDeviceProperty devProp;
            SCRSDK::CrError err = _getDeviceProperty(m_device_handle, codes[i], &devProp);
            if(err) break;
            if(devProp.GetValueType() == SCRSDK::CrDataType_STR) {

            } else {
                int64_t current = devProp.GetCurrentValue();
                if(current < 10) {
                    printf("  %s=%d\n", name.c_str(), (int)current);
                } else {
                    printf("  %s=0x%x(%d)\n", name.c_str(), (int)current, (int)current);
                }
            }
*/
        }
    }
};

CrString _getModelId(const SCRSDK::ICrCameraObjectInfo* objInfo)
{
    CrString id;
    if (CrString(objInfo->GetConnectionTypeName()) == CRSTR("IP")) {
        id = CrString(objInfo->GetMACAddressChar());
    } else {
        id = CrString((CrChar*)objInfo->GetId());
    }
    return CrString(objInfo->GetModel()).append(CRSTR(" (")).append(id).append(CRSTR(")"));
}

SCRSDK::CrError _getIdPassword(SCRSDK::ICrCameraObjectInfo* objInfo, std::string& fingerprint, std::string& userId, std::string& userPassword)
{
    char fpBuff[128] = {0};
    CrInt32u fpLen = 0;
    SCRSDK::CrError err = SCRSDK::GetFingerprint(objInfo, fpBuff, &fpLen);
    if(err) GotoError("", err);
    fingerprint = std::string(fpBuff, fpLen);

    std::cout << "fingerprint: " << fingerprint.c_str() << "\n";
    std::cout << "id:";       std::getline(std::cin, userId);
    std::cout << "password:"; std::getline(std::cin, userPassword);
    return 0;
Error:
    return err;
}

SCRSDK::CrError _afShooting(int64_t device_handle)
{
    int result = SCRSDK::CrError_Generic_Unknown;
    SCRSDK::CrError err = 0;
    std::promise<void> focusPromise;
    std::future<void> focusFuture = focusPromise.get_future();
    std::future_status status;

    SCRSDK::CrDeviceProperty devProp;

    setFocusPromise(&focusPromise);

    // S1 Locked
    err = _getDeviceProperty(device_handle, SCRSDK::CrDeviceProperty_S1, &devProp); if(err) GotoError("", err);
    devProp.SetCurrentValue(SCRSDK::CrLockIndicator_Locked);
    err = SCRSDK::SetDeviceProperty(device_handle, &devProp); if(err) GotoError("", err);

    // wait focusIndication
    status = focusFuture.wait_for(std::chrono::milliseconds(3000));
    if(status != std::future_status::ready) GotoError("timeout", 0);
    try{
        focusFuture.get();
    } catch(const std::exception&) GotoError("", 0);

    // S2 Locked / S1&S2 UnLocked
    err = SCRSDK::SendCommand(device_handle, SCRSDK::CrCommandId_Release, SCRSDK::CrCommandParam_Down); if(err) GotoError("", err);
    std::this_thread::sleep_for(std::chrono::milliseconds(35));
    err = SCRSDK::SendCommand(device_handle, SCRSDK::CrCommandId_Release, SCRSDK::CrCommandParam_Up); if(err) GotoError("", err);

    std::cout << "OK\n";
    result = 0;
Error:
    setFocusPromise(nullptr);
    return result;
}

SCRSDK::CrError _waitEvent(int64_t device_handle)
{
    int result = SCRSDK::CrError_Generic_Unknown;
    SCRSDK::CrError err = 0;
    std::promise<void> eventPromise;
    std::future<void> eventFuture = eventPromise.get_future();
    std::future_status status;

    setEventPromise(&eventPromise);

    // allow time for the camera to transfer the image to the host (large RAW files)
    status = eventFuture.wait_for(std::chrono::milliseconds(15000));
    if(status != std::future_status::ready) GotoError("download timeout", 0);

    try{
        eventFuture.get();
    } catch(const std::exception&) GotoError("", 0);

    std::cout << "OK\n";
    result = 0;
Error:
    setEventPromise(nullptr);
    return result;
}

std::vector<std::string> _split(std::string inputLine, char delimiter)
{
    std::vector<std::string> strArray;
    if (inputLine.empty()) return strArray;

    std::string tmp;
    std::stringstream ss{inputLine};
    while (getline(ss, tmp, delimiter)) {
        strArray.push_back(tmp);
    }
    return strArray;
}

void _dumpProp(int64_t h, uint32_t code, const char* name)
{
    int32_t nprop = 0;
    SCRSDK::CrDeviceProperty* list = nullptr;
    SCRSDK::CrError e = SCRSDK::GetSelectDeviceProperties(h, 1, &code, &list, &nprop);
    if(e || !list || nprop < 1) { printf("%s: query failed 0x%x\n", name, (int)e); if(list) SCRSDK::ReleaseDeviceProperties(h, list); return; }
    SCRSDK::CrDeviceProperty& p = list[0];
    printf("%s: enableFlag=%d setEnable=%d current=0x%llx valueType=%d valueSize=%u\n",
        name, (int)p.GetPropertyEnableFlag(), (int)p.IsSetEnableCurrentValue(),
        (unsigned long long)p.GetCurrentValue(), (int)p.GetValueType(), (unsigned)p.GetValueSize());
    CrInt8u* vals = p.GetValues();
    CrInt32u sz = p.GetValueSize();
    if(vals && sz) {
        printf("  allowed values: ");
        for(CrInt32u i = 0; i + 2 <= sz; i += 2) { CrInt16u v = *(CrInt16u*)(vals + i); printf("0x%x ", v); }
        printf("\n");
    }
    SCRSDK::ReleaseDeviceProperties(h, list);
}

void _dumpAll(int64_t h)
{
    SCRSDK::CrDeviceProperty* list = nullptr; int32_t n = 0;
    SCRSDK::CrError e = SCRSDK::GetDeviceProperties(h, &list, &n);
    printf("GetDeviceProperties err=0x%x count=%d\n", (int)e, (int)n);
    if(e || !list) return;
    bool sawDest = false;
    for(int i = 0; i < n; i++) {
        uint32_t code = list[i].GetCode();
        const char* nm = nullptr;
        switch(code) {
        case SCRSDK::CrDeviceProperty_StillImageStoreDestination: nm = "StillImageStoreDestination"; sawDest = true; break;
        case SCRSDK::CrDeviceProperty_MediaSLOT1_Status:          nm = "MediaSLOT1_Status"; break;
        case SCRSDK::CrDeviceProperty_MediaSLOT1_RemainingNumber: nm = "MediaSLOT1_Remaining"; break;
        case SCRSDK::CrDeviceProperty_MediaSLOT2_Status:          nm = "MediaSLOT2_Status"; break;
        case SCRSDK::CrDeviceProperty_RemoteSaveImageSize:        nm = "RemoteSaveImageSize"; break;
        default: break;
        }
        if(nm) printf("  %s (0x%x): current=0x%llx setEnable=%d enableFlag=%d\n",
            nm, code, (unsigned long long)list[i].GetCurrentValue(),
            (int)list[i].IsSetEnableCurrentValue(), (int)list[i].GetPropertyEnableFlag());
    }
    if(!sawDest) printf("  (StillImageStoreDestination NOT in property list -> unsupported on this body)\n");
    SCRSDK::ReleaseDeviceProperties(h, list);
}

int main(void)
{
    int result = -1;
    SCRSDK::CrError err = SCRSDK::CrError_None;
    SCRSDK::ICrEnumCameraObjectInfo* enumCameraObjectInfo = nullptr;
    SCRSDK::ICrCameraObjectInfo* objInfo = nullptr;
    DeviceCallback deviceCallback;

  #if defined(__APPLE__)
    #define MAC_MAX_PATH 255
    char pathBuf[MAC_MAX_PATH] = {0};
    if(NULL == getcwd(pathBuf, sizeof(pathBuf) - 1)) return 1;
    CrString path = pathBuf;
  #else
    CrString path = fs::current_path().native();
  #endif

    bool boolRet = SCRSDK::Init();
    if(!boolRet) GotoError("", 0);

    // enumeration
    {
        uint32_t count = 0;
        uint32_t index = 1;

        err = SCRSDK::EnumCameraObjects(&enumCameraObjectInfo, 3/*timeInSec*/);
        if(err || !enumCameraObjectInfo) GotoError("no camera", err);

        count = enumCameraObjectInfo->GetCount();
        if(count >= 2) {
            for (uint32_t i = 0; i < count; ++i) {
                auto* info = enumCameraObjectInfo->GetCameraObjectInfo(i);
                CrCout << '[' << i + 1 << "] " << _getModelId(info) << "\n";
            }

            std::string inputLine;
            std::cout << "select camera:"; std::getline(std::cin, inputLine);
            try { index = stoi(inputLine); } catch(const std::exception&) { GotoError("", 0); }
            if(index < 1 || index > count) GotoError("", 0);
        }
        objInfo = (SCRSDK::ICrCameraObjectInfo*)enumCameraObjectInfo->GetCameraObjectInfo(index - 1);
        m_modelId = _getModelId(objInfo);
    }

    // connect
    {
        std::string  fingerprint = "";
        std::string  userId = "";
        std::string  userPassword = "";
        std::promise<void> eventPromise;
        std::future<void> eventFuture = eventPromise.get_future();

        if (objInfo->GetSSHsupport() == SCRSDK::CrSSHsupport_ON) {
            err = _getIdPassword(objInfo, fingerprint, userId, userPassword); if(err) goto Error;
        }

        setEventPromise(&eventPromise);
        err = SCRSDK::Connect(objInfo, &deviceCallback, &m_device_handle,
            SCRSDK::CrSdkControlMode_Remote,
            SCRSDK::CrReconnecting_OFF,
            userId.c_str(), userPassword.c_str(), fingerprint.c_str(), (uint32_t)fingerprint.size());
        if(err) GotoError("", err);

    //  std::future_status status = eventFuture.wait_for(std::chrono::milliseconds(3000));
    //  if(status != std::future_status::ready) GotoError("timeout",0);
        try{
            eventFuture.get();
        } catch(const std::exception&) GotoError("", 0);
    }

    // set the download destination folder on the Pi.
    // NOTE: the A7R IV does not expose StillImageStoreDestination over the SDK,
    // so the camera must be set to send images to the PC via its own menu:
    //   MENU > Network > PC Remote Function > Still Img. Save Dest. = "PC Only" (or "PC+Camera")
    {
        path = CRSTR("/root/photos");
        CrCout << "save path=" << path.data() << "\n";
        err = SCRSDK::SetSaveInfo(m_device_handle, const_cast<CrChar*>(path.data()), const_cast<CrChar*>(CRSTR("booth")), -1/*startNo*/);
        if(err) GotoError("SetSaveInfo", err);
    }

    std::this_thread::sleep_for(std::chrono::milliseconds(1500));

    // dump camera state now that properties are loaded
    _dumpAll(m_device_handle);

    // route captures to the Pi only (card shows 0 remaining; PC+Camera would abort the save).
    // Settable now that the property list has loaded (immediately after connect it returns Api_InvalidCalled).
    {
        SCRSDK::CrDeviceProperty dst;
        dst.SetCode(SCRSDK::CrDeviceProperty_StillImageStoreDestination);
        dst.SetValueType(SCRSDK::CrDataType_UInt16);
        dst.SetCurrentValue(SCRSDK::CrStillImageStoreDestination_HostPC);
        SCRSDK::CrError de = SCRSDK::SetDeviceProperty(m_device_handle, &dst);
        printf("set StoreDestination=HostPC -> 0x%x\n", (int)de);

        // send a small (~2MP) JPEG to the host instead of the full 61MP file,
        // so the transfer survives the USB link (full RAW drops the connection).
        SCRSDK::CrDeviceProperty ts;
        ts.SetCode(SCRSDK::CrDeviceProperty_Still_Image_Trans_Size);
        ts.SetValueType(SCRSDK::CrDataType_UInt16);
        ts.SetCurrentValue(SCRSDK::CrPropertyStillImageTransSize_SmallSize);
        SCRSDK::CrError te = SCRSDK::SetDeviceProperty(m_device_handle, &ts);
        printf("set Still_Image_Trans_Size=Small -> 0x%x\n", (int)te);

        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
    }

    // fire a single shot; the image downloads to /root/photos
    std::cout << "Capturing...\n";
    err = _afShooting(m_device_handle);
    if(err) goto Error;
    err = _waitEvent(m_device_handle);   // OnCompleteDownload fulfils this when the file lands on the Pi
    if(err) goto Error;

    result = 0;
Error:
    if(enumCameraObjectInfo) enumCameraObjectInfo->Release();

    if(m_connected) {
        std::promise<void> eventPromise;
        std::future<void> eventFuture = eventPromise.get_future();
        setEventPromise(&eventPromise);
        SCRSDK::Disconnect(m_device_handle);
        eventFuture.wait_for(std::chrono::milliseconds(3000));
    }
    if(m_device_handle) SCRSDK::ReleaseDevice(m_device_handle);
    SCRSDK::Release();

    return result;
}
