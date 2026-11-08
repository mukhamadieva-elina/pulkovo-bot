"""Кэш имени пользователя VK; расширенные данные профиля не запрашиваются."""

import time


class ProfileSync:
    def __init__(self, db, api):
        self.db,self.api=db,api

    def refresh(self):
        now=time.time()
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            ids=[r['user_id'] for r in con.execute('SELECT DISTINCT c.user_id FROM conversations c LEFT JOIN vk_profiles p ON p.user_id=c.user_id WHERE c.user_id>0 AND c.user_id<2000000000 AND coalesce(p.updated_at,0)<? AND coalesce(p.last_checked_at,0)<? ORDER BY c.user_id LIMIT 100',(now-86400,now-300))]
            con.executemany('INSERT INTO vk_profiles(user_id,last_checked_at) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_checked_at=excluded.last_checked_at',[(ident,now) for ident in ids])
        if not ids:
            return 0
        profiles=self.api.users.get(user_ids=','.join(map(str,ids)))
        changed=0
        with self.db.connect() as con:
            for profile in profiles:
                if not isinstance(profile,dict) or profile.get('id') not in ids:
                    continue
                first=str(profile.get('first_name') or '').strip()[:150]
                last=str(profile.get('last_name') or '').strip()[:150]
                con.execute('UPDATE vk_profiles SET first_name=?,last_name=?,updated_at=? WHERE user_id=?',(first,last,now,profile['id']))
                changed+=1
        return changed
