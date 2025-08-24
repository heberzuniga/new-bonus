import streamlit as st
import pandas as pd
import numpy as np
import json, os, math, time
from datetime import datetime

APP_TITLE = "Misión Bonos — MVP (rápido)"
STATE_DIR = "games"
os.makedirs(STATE_DIR, exist_ok=True)

def game_path(code): return os.path.join(STATE_DIR, f"{code}.json")
def load_state(code):
    p = game_path(code)
    if os.path.exists(p):
        return json.load(open(p, "r", encoding="utf-8"))
    return {
        "game": {"game_code": code, "rondas_totales": 3, "ronda_actual": 1, "estado": "LOBBY",
                 "fraccion_anio": 0.25, "bid_bp": 20, "ask_bp": 20, "comision_bps": 10,
                 "cash_inicial": 1_000_000.0, "created_at": datetime.utcnow().isoformat()},
        "bonds": [], "events": [], "prices": [], "teams": [], "orders": [], "ledger": []
    }
def save_state(s): json.dump(s, open(game_path(s["game"]["game_code"]), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def price_bond_mid(b, y, frac, rounds_elapsed):
    f = int(b.get("frecuencia_anual",2) or 2); V = float(b.get("valor_nominal",1000))
    c = float(b.get("tasa_cupon_anual",0.08)); T0 = float(b.get("vencimiento_anios",3))
    Tre = max(0.0, T0 - rounds_elapsed*frac); N = max(1, int(math.ceil(Tre * f)))
    C = V * (c/f); i = y/f
    return float(sum(C/((1+i)**k) for k in range(1,N+1)) + V/((1+i)**N))

def bid_ask(mid, bid_bp, ask_bp):
    return mid*(1-bid_bp/10_000), mid*(1+ask_bp/10_000)

def eff_ytm(spread_bps, delta_mkt_bps, idios_bps): return (spread_bps + delta_mkt_bps + idios_bps)/10_000.0

BOND_COLS = ["type","bond_id","nombre","valor_nominal","tasa_cupon_anual","frecuencia_anual","vencimiento_anios","spread_bps","callable","precio_call","round","delta_tasa_bps","impacto_bps","descripcion"]
def load_csv(file):
    try: df = pd.read_csv(file)
    except Exception: df = pd.read_csv(file, sep=";")
    df.columns = [c.lower().strip() for c in df.columns]
    for c in BOND_COLS:
        if c not in df.columns: df[c] = np.nan
    return df[BOND_COLS]

def apply_scenario(df, s):
    bonds, events = [], []
    for _,r in df.iterrows():
        t = str(r["type"]).upper()
        if t=="BOND":
            bonds.append({"bond_id":str(r["bond_id"]), "nombre":str(r["nombre"]), "valor_nominal":float(r.get("valor_nominal",1000) or 1000),
                          "tasa_cupon_anual":float(r.get("tasa_cupon_anual",0.08) or 0.08), "frecuencia_anual":int(r.get("frecuencia_anual",2) or 2),
                          "vencimiento_anios":float(r.get("vencimiento_anios",3) or 3), "spread_bps":float(r.get("spread_bps",0) or 0)})
        elif t in ("MARKET","IDIOS"):
            events.append({"round":int(r.get("round",1) or 1), "tipo":t, "bond_id":(str(r.get("bond_id")) if t=="IDIOS" else None),
                           "delta_tasa_bps":float(r.get("delta_tasa_bps",0) or 0), "impacto_bps":float(r.get("impacto_bps",0) or 0), "publicado":False})
    s["bonds"], s["events"] = bonds, events

def publish_prices(s):
    g=s["game"]; r=g["ronda_actual"]; frac=g["fraccion_anio"]; bid_bp=g["bid_bp"]; ask_bp=g["ask_bp"]
    dmkt = sum(e.get("delta_tasa_bps",0) for e in s["events"] if e["round"]==r and e["tipo"]=="MARKET")
    new=[]
    for b in s["bonds"]:
        idios = sum(e.get("impacto_bps",0) for e in s["events"] if e["round"]==r and e["tipo"]=="IDIOS" and str(e.get("bond_id"))==str(b["bond_id"]))
        y = eff_ytm(b.get("spread_bps",0), dmkt, idios); mid = price_bond_mid(b, y, frac, r-1); bd, ak = bid_ask(mid, bid_bp, ask_bp)
        new.append({"ronda":r,"bond_id":b["bond_id"],"y_efectiva":y,"precio_mid":mid,"precio_bid":bd,"precio_ask":ak,"ts_publicacion":datetime.utcnow().isoformat()})
    for e in s["events"]:
        if e["round"]==r: e["publicado"]=True
    s["prices"] = [p for p in s["prices"] if p["ronda"]!=r] + new
    g["estado"]="TRADING_ON"

def get_prices_mid(s, ronda): return {p["bond_id"]:p["precio_mid"] for p in s["prices"] if p["ronda"]==ronda}

def team_get(s, name):
    for t in s["teams"]:
        if t["team_name"]==name: return t
    return None

def team_register(s, name, pin=""):
    if team_get(s,name): return False, "El equipo ya existe"
    cash0 = s["game"]["cash_inicial"]
    t={"team_id":f"T{len(s['teams'])+1}","team_name":name,"pin":pin,"cash_inicial":cash0,"activo":True,"created_at":datetime.utcnow().isoformat()}
    s["teams"].append(t); return True, f"Equipo {name} creado con {cash0:,.2f} de cash"

def team_state(s, name):
    t=team_get(s,name); 
    if not t: return {}, 0.0
    pos={}, float(t["cash_inicial"])
    pos = {}
    cash = float(t["cash_inicial"])
    for o in s["orders"]:
        if o["team_id"]!=t["team_id"]: continue
        qty=float(o["qty"]); px=float(o["price_exec"]); fees=float(o["fees"])
        if o["side"]=="BUY":
            pos[o["bond_id"]]=pos.get(o["bond_id"],0.0)+qty; cash -= qty*px + fees
        else:
            pos[o["bond_id"]]=pos.get(o["bond_id"],0.0)-qty; cash += qty*px - fees
    return pos, cash

def can_exec(s, name, side, bond_id, qty):
    g=s["game"]; r=g["ronda_actual"]; fees_bps=g["comision_bps"]
    p=next((p for p in s["prices"] if p["ronda"]==r and p["bond_id"]==bond_id), None)
    if not p: return False,"No hay precios",0,0
    px=p["precio_ask"] if side=="BUY" else p["precio_bid"]; fees=qty*px*fees_bps/10_000
    pos,cash = team_state(s,name)
    if side=="BUY" and cash < qty*px + fees: return False,"Cash insuficiente",px,fees
    if side=="SELL" and pos.get(bond_id,0)<qty: return False,"Posición insuficiente",px,fees
    return True,"OK",px,fees

def exec_order(s, name, side, bond_id, qty):
    ok,msg,px,fees = can_exec(s,name,side,bond_id,qty)
    if not ok: return False,msg
    t=team_get(s,name)
    s["orders"].append({"ts":datetime.utcnow().isoformat(),"team_id":t["team_id"],"bond_id":bond_id,"side":side,"qty":int(qty),"price_exec":px,"fees":fees,"ronda":s["game"]["ronda_actual"]})
    return True,f"Orden {side} {qty} {bond_id} @ {px:,.2f} (fees {fees:,.2f})"

def leaderboard(s, ronda=None):
    if ronda is None: ronda = s["game"]["ronda_actual"]
    mid = get_prices_mid(s, ronda); rows=[]
    for t in s["teams"]:
        pos,cash=team_state(s,t["team_name"])
        val_pos=sum(q*mid.get(b,0) for b,q in pos.items())
        rows.append({"Equipo":t["team_name"],"Cash":cash,"Valor_Posiciones":val_pos,"Valor_Portafolio":cash+val_pos})
    df=pd.DataFrame(rows) if rows else pd.DataFrame(columns=["Equipo","Cash","Valor_Posiciones","Valor_Portafolio"])
    return df.sort_values("Valor_Portafolio", ascending=False).reset_index(drop=True)

def final_ranking(s):
    if not s["prices"]: return pd.DataFrame(columns=["Equipo","Valor_Final","Rentabilidad_%"])
    last=max(p["ronda"] for p in s["prices"]); mid=get_prices_mid(s,last); rows=[]
    for t in s["teams"]:
        pos,cash=team_state(s,t["team_name"]); val_pos=sum(q*mid.get(b,0) for b,q in pos.items())
        valor=cash+val_pos; base=float(t["cash_inicial"] or 1.0); rent=(valor/base-1.0)*100
        rows.append({"Equipo":t["team_name"],"Valor_Final":valor,"Rentabilidad_%":rent})
    return pd.DataFrame(rows).sort_values("Rentabilidad_%", ascending=False).reset_index(drop=True)

def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    with st.sidebar:
        code = st.text_input("Game code", "MB-001")
        role = st.radio("Rol", ["Participante","Moderador"])
        auto = st.checkbox("Auto‑refresh (3s)", value=False)
        if auto: time.sleep(3); st.rerun()

    s = load_state(code)

    if role=="Moderador":
        g=s["game"]
        st.subheader("Moderador")
        c1,c2,c3=st.columns(3)
        with c1:
            g["rondas_totales"]=st.number_input("Rondas totales",1,20,int(g["rondas_totales"]))
            g["fraccion_anio"]=st.number_input("Fracción de año/ronda",0.05,1.0,float(g["fraccion_anio"]),0.05)
        with c2:
            g["bid_bp"]=st.number_input("Bid (bps)",0,500,int(g["bid_bp"]))
            g["ask_bp"]=st.number_input("Ask (bps)",0,500,int(g["ask_bp"]))
        with c3:
            g["comision_bps"]=st.number_input("Comisión (bps)",0,200,int(g["comision_bps"]))
            g["cash_inicial"]=st.number_input("Cash inicial",1000.0,1e8,float(g["cash_inicial"]),1000.0,format="%.2f")

        st.markdown("### 1) Cargar escenario")
        up = st.file_uploader("CSV (o usa el ejemplo)", type=["csv"])
        if st.button("Cargar CSV") and up is not None:
            df=load_csv(up); apply_scenario(df,s); save_state(s); st.success(f"Cargados {len(s['bonds'])} bonos y {len(s['events'])} eventos.")
        if st.button("Usar ejemplo"):
            demo=pd.read_csv("assets/sample_escenario.csv"); apply_scenario(demo,s); save_state(s); st.success("Ejemplo cargado")

        st.markdown("### 2) Ronda")
        st.write(f"Ronda: **{g['ronda_actual']}** / {g['rondas_totales']} · Estado: **{g['estado']}**")
        colx,coly,colz=st.columns(3)
        with colx:
            if st.button("Publicar precios de ronda"):
                if not s["bonds"]: st.error("Primero carga un escenario")
                else: publish_prices(s); save_state(s); st.success("Precios publicados (TRADING_ON)")
        with coly:
            if st.button("Cerrar TRADING"):
                g["estado"]="TRADING_OFF"; save_state(s)
        with colz:
            if g["estado"]=="TRADING_OFF":
                if g["ronda_actual"]<g["rondas_totales"] and st.button("Avanzar ronda ➡️"):
                    g["ronda_actual"]+=1; g["estado"]="LOBBY"; save_state(s)
                elif g["ronda_actual"]>=g["rondas_totales"] and st.button("Finalizar (FIN)"):
                    g["estado"]="FIN"; save_state(s)

        t1,t2,t3 = st.tabs(["Bonos","Eventos","Precios"])
        with t1: st.dataframe(pd.DataFrame(s["bonds"]), use_container_width=True)
        with t2: st.dataframe(pd.DataFrame(s["events"]).sort_values(["round","tipo"]), use_container_width=True)
        with t3: st.dataframe(pd.DataFrame(s["prices"]).sort_values(["ronda","bond_id"]), use_container_width=True)

        st.markdown("### Leaderboard (ronda actual)")
        st.dataframe(leaderboard(s), use_container_width=True)

    else:
        st.subheader("Participante")
        g=s["game"]; st.caption(f"Ronda {g['ronda_actual']} — Estado {g['estado']}")
        with st.form("reg"):
            name=st.text_input("Nombre del equipo"); pin=st.text_input("PIN (opcional)", type="password")
            ok=st.form_submit_button("Registrar/Ingresar")
        if ok and name.strip():
            t=team_get(s,name)
            if t is None:
                ok2,msg=team_register(s,name.strip(),pin.strip()); st.success(msg if ok2 else "No se pudo registrar"); save_state(s)
            else:
                if t.get("pin","") and t["pin"]!=pin: st.error("PIN incorrecto")
                else: st.success(f"Bienvenido, {name}!")
        team = st.session_state.get("team") or (name.strip() if ok and name.strip() else None)
        if team: st.session_state["team"]=team

        if "team" not in st.session_state:
            st.info("Ingresa tu equipo para operar. Abajo ves el ranking en vivo.")
            st.dataframe(leaderboard(s), use_container_width=True)
            return

        team = st.session_state["team"]
        pos,cash = team_state(s, team)
        c1,c2 = st.columns(2)
        with c1: st.metric("Cash", f"{cash:,.2f}")
        with c2:
            prices_mid = get_prices_mid(s, g["ronda_actual"])
            val_pos = sum(q*prices_mid.get(b,0) for b,q in pos.items())
            st.metric("Valor posiciones (mid)", f"{val_pos:,.2f}")
        st.write("Posiciones"); st.dataframe(pd.DataFrame([{"Bono":b,"Qty":q} for b,q in pos.items()]) if pos else pd.DataFrame(columns=["Bono","Qty"]), use_container_width=True)
        st.write("Precios de la ronda"); st.dataframe(pd.DataFrame([p for p in s["prices"] if p["ronda"]==g["ronda_actual"]]), use_container_width=True)

        st.write("Órdenes")
        if g["estado"]!="TRADING_ON": st.warning("Mercado cerrado por ahora.")
        col1,col2,col3,col4 = st.columns(4)
        with col1: side=st.selectbox("Side", ["BUY","SELL"])
        with col2: bond_id=st.selectbox("Bono", [b["bond_id"] for b in s["bonds"]])
        with col3: qty=st.number_input("Cantidad", min_value=1, value=1, step=1)
        with col4:
            if st.button("Enviar orden", disabled=g["estado"]!="TRADING_ON"):
                ok,msg=exec_order(s, team, side, bond_id, int(qty))
                if ok: st.success(msg); save_state(s)
                else: st.error(msg)

        st.write("Leaderboard")
        st.dataframe(leaderboard(s), use_container_width=True)

    st.markdown("---")
    st.subheader("Resultados por equipo (ronda actual)")
    st.dataframe(leaderboard(s), use_container_width=True)
    st.subheader("Ranking final por rentabilidad (%)")
    st.dataframe(final_ranking(s), use_container_width=True)

if __name__ == "__main__":
    main()