#!/usr/bin/env python3
"""Workout logger. Usage:
 coach.py next | start | set EXERCISE TARGET WEIGHT UNIT REPS [NOTE] | undo | end [NOTE] | status | history EXERCISE [N] | progress"""
import sys, os, datetime as dt, openpyxl
from openpyxl.styles import Font
BOOK=os.environ.get("BOOK",os.path.join(os.path.dirname(os.path.abspath(__file__)),"DUP_Workout_Log.xlsx"))
ORDER=["Legs 1","Push 1","Pull 1","Legs 2","Push 2","Pull 2"]
wb=openpyxl.load_workbook(BOOK); S,T,P=wb["Sessions"],wb["Sets"],wb["Program"]
A=Font(name="Arial")
def sessions(): return [r for r in range(2,S.max_row+1) if S.cell(r,1).value is not None]
def open_row():
    rs=sessions()
    return rs[-1] if rs and S.cell(rs[-1],4).value is None else None
def nxt():
    done=[(S.cell(r,6).value,S.cell(r,7).value) for r in sessions() if S.cell(r,4).value is not None]
    if not done: return 1,ORDER[0]
    w,d=done[-1]; i=ORDER.index(d)+1
    return (w,ORDER[i]) if i<6 else (w+1,ORDER[0])
def plan(w,d):
    for r in P.iter_rows(min_row=2,values_only=True):
        if r[0]==w and r[1]==d: print(f"  {r[2]}. {r[3]}: {r[4]}"+(f" ({r[5]})" if r[5] else ""))
def put(ws,row,vals):
    for i,v in enumerate(vals,1):
        ws.cell(row,i,v).font=A
def totals(r):
    n=S.cell(r,1).value
    S.cell(r,8,f"=COUNTIF(Sets!$A$2:$A$5000,A{r})").font=A
    S.cell(r,9,f"=SUMIF(Sets!$A$2:$A$5000,A{r},Sets!$K$2:$K$5000)").font=A
c=sys.argv[1]; a=sys.argv[2:]; now=dt.datetime.now()
if c=="next":
    w,d=nxt(); print(f"Week {w} {d}"); plan(w,d)
elif c=="start":
    if open_row(): sys.exit("A workout is already open")
    w,d=nxt(); r=S.max_row+1; n=len(sessions())+1
    put(S,r,[n,now.strftime("%Y-%m-%d"),now.strftime("%H:%M"),None,None,w,d]); totals(r)
    print(f"Session {n} started {now:%Y-%m-%d %H:%M}: Week {w} {d}"); plan(w,d)
elif c=="set":
    r=open_row()
    if not r: sys.exit("No open workout")
    ex,tg,wt,un,rp=a[:5]; note=a[5] if len(a)>5 else None
    n=S.cell(r,1).value; wt=float(wt); rp=int(rp)
    k=sum(1 for x in range(2,T.max_row+1) if T.cell(x,1).value==n and T.cell(x,5).value==ex and T.cell(x,6).value==tg)+1
    x=T.max_row+1
    put(T,x,[n,S.cell(r,2).value,S.cell(r,6).value,S.cell(r,7).value,ex,tg,k,wt,un,rp,f"=H{x}*J{x}",
             f'=IF(RIGHT(F{x},2)="RM",IF(J{x}=1,H{x},ROUND(H{x}*(1+J{x}/30),1)),"")',note])
    print(f"Logged {ex} {tg} set {k}: {wt:g} {un} x {rp}")
elif c=="undo":
    x=T.max_row
    if x<2: sys.exit("Nothing to undo")
    print("Removed:",[T.cell(x,i).value for i in (5,6,7,8,10)]); T.delete_rows(x)
elif c=="end":
    r=open_row()
    if not r: sys.exit("No open workout")
    st=dt.datetime.strptime(f"{S.cell(r,2).value} {S.cell(r,3).value}","%Y-%m-%d %H:%M")
    S.cell(r,4,now.strftime("%H:%M")).font=A; S.cell(r,5,int((now-st).total_seconds()//60)).font=A
    if a: S.cell(r,10,a[0]).font=A
    print(f"Session {S.cell(r,1).value} closed at {now:%H:%M}")
elif c=="history":
    rows=[[T.cell(x,i).value for i in (2,3,4,6,7,8,9,10)] for x in range(2,T.max_row+1) if T.cell(x,5).value==a[0]]
    for r in rows[-int(a[1]) if len(a)>1 else -12:]: print(f"{r[0]} W{r[1]} {r[2]} {r[3]} set {r[4]}: {r[5]:g} {r[6]} x {r[7]}")
    if not rows: print("No history for",a[0])
elif c=="progress":
    done=sum(1 for r in sessions() if S.cell(r,4).value is not None)
    sets=[[T.cell(x,i).value for i in (5,6,8,9,10)] for x in range(2,T.max_row+1) if T.cell(x,1).value is not None]
    print(f"Workouts: {done}/54 | Sets: {len(sets)} | Volume: {sum(s[2]*s[4] for s in sets):,.0f}")
    for l in ("Squat","Bench","Deadlift","OHP"):
        rm=[s for s in sets if s[0]==l and s[1].endswith("RM")]
        if rm:
            b=max(rm,key=lambda s:s[2] if s[4]==1 else s[2]*(1+s[4]/30))
            e=b[2] if b[4]==1 else b[2]*(1+b[4]/30)
            print(f"{l}: est. 1RM {e:.1f} {b[3]} (from {b[2]:g} x {b[4]})")
elif c=="status":
    r=open_row(); print("Open:" if r else "No open workout", [S.cell(r,i).value for i in range(1,8)] if r else "")
    w,d=nxt(); print(f"Next: Week {w} {d}")
wb.save(BOOK)
