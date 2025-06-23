#!/usr/bin/env python3

import time
import pandas as pd
from skaha.session import Session

def get_open_sessions():
    """
    Fetch and return a pandas DataFrame of all open CANFAR sessions.
    Columns: type, status, startTime, name, id
    """
    session = Session()
    open_sessions = session.fetch()

    if not open_sessions:
        return pd.DataFrame(columns=['type', 'status', 'startTime', 'name', 'id'])

    df = pd.DataFrame([{
        'type': s.get('type', ''),
        'status': s.get('status', ''),
        'startTime': s.get('startTime', ''),
        'name': s.get('name', ''),
        'id': s.get('id', '')
    } for s in open_sessions])

    df = df.sort_values(by='startTime', ascending=False).reset_index(drop=True)
    return df

def kill_headless_sessions(pause_seconds=1):
    """
    Find all running sessions labeled as 'headless' and destroy them.
    Optionally pauses `pause_seconds` between each destroy call.
    """
    session = Session()
    df_sessions = get_open_sessions()

    if df_sessions.empty:
        print("No open sessions found.")
        return

    # Filter for headless sessions that are still running
    mask = (
        (df_sessions['type'].str.lower() == 'headless') &
        (df_sessions['status'].str.lower() == 'running')
    )
    headless_running = df_sessions[mask]

    if headless_running.empty:
        print("No running 'headless' sessions to kill.")
        return

    for idx, row in headless_running.iterrows():
        sess_id = row['id']
        sess_name = row['name']
        try:
            session.destroy(sess_id)
            print(f"Killed headless session: name='{sess_name}', id='{sess_id}'")
        except Exception as e:
            print(f"Failed to kill session id='{sess_id}': {e}")
        time.sleep(pause_seconds)

def main():
    """
    Entry point: kill all running 'headless' CANFAR sessions.
    """
    kill_headless_sessions()

if __name__ == "__main__":
    main()

