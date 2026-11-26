; =============================================================================
; AgentBridge.ahk - lets external automation (e.g. the pmc-mcp MCP server used by
; AI agents) control a running instance of Pulover's Macro Creator.
;
; Transport: WM_COPYDATA sent to A_ScriptHwnd with dwData = 0x504D4341 ("PMCA").
; The payload is a UTF-16 JSON request:
;     {"id": "<[A-Za-z0-9_-]{1,64}>", "cmd": "<command>", "args": {...}}
; The handler only queues the request and returns 1 immediately (WM_COPYDATA is
; synchronous for the sender). The request is processed on a timer thread and
; the JSON response is written atomically to:
;     %A_Temp%\PMC_Agent\<id>.json
;     {"id": ..., "ok": 1, "result": {...}}   or   {"id": ..., "ok": 0, "error": "..."}
;
; The bridge is OFF unless PMC is started with the "-agent" command-line switch
; or [Options] AgentBridge=1 is set in MacroCreator.ini. When it is on, PMC
; writes a discovery file %A_Temp%\PMC_Agent\bridge.json with its window handle.
;
; Commands: ping, status, open, save, get_macro, select, play, stop,
;           record_start, record_stop, show, hide
; =============================================================================

AgentBridge_Start()
{
	global AgentBridgeOn, AgentBridgeDir, CurrentVersion, CloseAfterPlay
	static PMCA := 0x504D4341

	If (CloseAfterPlay) ; headless one-shot run (-s): never take over the bridge
		AgentBridgeOn := 0
	If (!AgentBridgeOn)
		return
	AgentBridgeDir := A_Temp "\PMC_Agent"
	FileCreateDir, %AgentBridgeDir%
	Info := {"hwnd": A_ScriptHwnd + 0
		, "pid": DllCall("GetCurrentProcessId", "UInt")
		, "version": CurrentVersion
		, "protocol": 1
		, "dwData": PMCA}
	AgentBridge_WriteFile(AgentBridgeDir "\bridge.json", JSON.Dump(Info))
	OnExit("AgentBridge_Stop")
}

AgentBridge_Stop(ExitReason := "", ExitCode := "")
{
	global AgentBridgeDir
	FileDelete, %AgentBridgeDir%\bridge.json
	return 0 ; let the script exit
}

; Called from Receive_Params() when dwData matches. Must return quickly.
AgentBridge_Receive(lParam)
{
	global AgentBridgeOn

	If (!AgentBridgeOn)
		return 0
	Size := NumGet(lParam + A_PtrSize, "UInt")
,	Address := NumGet(lParam + 2*A_PtrSize)
	If ((!Address) || (Size < 2) || (Size > 4*1024*1024))
		return 0
	Text := StrGet(Address, Size // 2, "UTF-16")
	AgentBridge_Queue("push", Text)
	fn := Func("AgentBridge_Process")
	SetTimer, % fn, -1
	return 1
}

AgentBridge_Queue(Op, Item := "")
{
	static Queue := []
	If (Op = "push")
		return Queue.Push(Item)
	return Queue.Length() ? Queue.RemoveAt(1) : ""
}

AgentBridge_Process()
{
	global AgentBridgeDir

	While ((Text := AgentBridge_Queue("pop")) != "")
	{
		ReqId := ""
		Try
		{
			Req := JSON.Load(Text)
			ReqId := Req.id
			If (!RegExMatch(ReqId, "^[A-Za-z0-9_-]{1,64}$"))
				continue ; nowhere safe to answer
			Args := IsObject(Req.args) ? Req.args : {}
			Result := AgentBridge_Dispatch(Req.cmd, Args)
			Response := {"id": ReqId, "ok": 1, "result": Result}
		}
		Catch e
		{
			If (!RegExMatch(ReqId, "^[A-Za-z0-9_-]{1,64}$"))
				continue
			Response := {"id": ReqId, "ok": 0, "error": IsObject(e) ? e.Message : e}
		}
		AgentBridge_WriteFile(AgentBridgeDir "\" ReqId ".json", JSON.Dump(Response))
	}
}

AgentBridge_Dispatch(Cmd, Args)
{
	global
	local Path, Tab, Data, _ext

	If (Cmd = "ping")
		return {"pong": 1, "version": CurrentVersion, "pid": DllCall("GetCurrentProcessId", "UInt")}
	If (Cmd = "status")
		return AgentBridge_Status()
	If (Cmd = "open")
	{
		Path := Args.path
		If (!FileExist(Path) || InStr(FileExist(Path), "D"))
			throw Exception("File not found: " Path)
		Gui, 1:Submit, NoHide
		GoSub, SaveData
		If ((ListCount > 0) && (SavePrompt) && (!Args.discard_changes))
			throw Exception("unsaved_changes: the open project has unsaved changes; pass discard_changes=true or save first")
		If (PlayOSOn || Record)
			throw Exception("busy: stop playback/recording first")
		Files := Path
		GoSub, OpenFile
		return AgentBridge_Status()
	}
	If (Cmd = "save")
	{
		If (Args.path != "")
		{
			Path := Args.path
			SplitPath, Path,,, _ext
			If (_ext != "pmc")
				throw Exception("save path must end in .pmc")
			CurrentFileName := Args.path
		}
		If (CurrentFileName = "")
			throw Exception("no file name: pass path")
		GoSub, Save
		return {"file": CurrentFileName}
	}
	If (Cmd = "get_macro")
	{
		Tab := AgentBridge_Tab(Args.macro)
		Data := PMC.LVGet("InputList" Tab)
		Gui, chMacro:ListView, InputList%A_List%
		return {"macro": Tab, "name": CopyMenuLabels[Tab], "rows": Data.Row}
	}
	If (Cmd = "select")
	{
		AgentBridge_Select(AgentBridge_Tab(Args.macro))
		return AgentBridge_Status()
	}
	If (Cmd = "play")
	{
		If (PlayOSOn || Record)
			throw Exception("busy: playback or recording already in progress")
		Tab := AgentBridge_Tab(Args.macro)
		If (ListCount%Tab% = 0)
			throw Exception("macro " Tab " is empty")
		If (InStr(CopyMenuLabels[Tab], "()"))
			throw Exception("macro " Tab " is a function and cannot be played directly")
		AgentBridge_Select(Tab)
		GoSub, TestRun
		return {"started": 1, "macro": Tab}
	}
	If (Cmd = "stop")
	{
		GoSub, f_AbortKey
		return {"stopped": 1}
	}
	If (Cmd = "record_start")
	{
		If (PlayOSOn)
			throw Exception("busy: playback in progress")
		If (Record)
			return {"recording": 1}
		If (Args.macro != "")
			AgentBridge_Select(AgentBridge_Tab(Args.macro))
		DontShowRec := 1
		GoSub, RecStart
		return {"recording": Record ? 1 : 0, "macro": A_List}
	}
	If (Cmd = "record_stop")
	{
		If (Record)
			GoSub, RecStart ; RecStart toggles recording off and commits the rows
		return {"recording": 0, "macro": A_List, "rows": ListCount%A_List%}
	}
	If (Cmd = "show")
	{
		Gui, 1:Show
		return {"visible": 1}
	}
	If (Cmd = "hide")
	{
		Gui, 1:Show, Hide
		return {"visible": 0}
	}
	throw Exception("unknown command: " Cmd)
}

AgentBridge_Status()
{
	global
	local Macros := [], i

	Loop, %TabCount%
	{
		i := A_Index
		Macros.Push({"index": i, "name": CopyMenuLabels[i], "rows": ListCount%i% + 0
			, "hotkey": o_AutoKey[i], "manual_key": o_ManKey[i], "repeat": o_TimesG[i]
			, "is_function": InStr(CopyMenuLabels[i], "()") ? 1 : 0})
	}
	return {"version": CurrentVersion, "file": CurrentFileName, "unsaved": SavePrompt ? 1 : 0
		, "playing": PlayOSOn ? 1 : 0, "recording": Record ? 1 : 0
		, "active_macro": A_List + 0, "macros": Macros}
}

; Accepts a 1-based index or a macro (tab) name. Empty = active tab.
AgentBridge_Tab(Macro)
{
	global
	If (Macro = "")
		return A_List
	If Macro is integer
	{
		If ((Macro >= 1) && (Macro <= TabCount))
			return Macro
		throw Exception("macro index out of range: " Macro " (1-" TabCount ")")
	}
	Loop, %TabCount%
		If (CopyMenuLabels[A_Index] = Macro)
			return A_Index
	throw Exception("macro not found: " Macro)
}

AgentBridge_Select(Tab)
{
	global
	If (Tab = A_List)
		return
	GuiControl, chMacro:Choose, A_List, %Tab%
	GoSub, TabSel
}

AgentBridge_WriteFile(Path, Text)
{
	Tmp := Path ".tmp"
	FileDelete, %Tmp%
	FileAppend, %Text%, %Tmp%, UTF-8-RAW
	FileMove, %Tmp%, %Path%, 1
}
