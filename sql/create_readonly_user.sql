-- 建立 MCP 專用的唯讀帳號。請由資料庫管理員自行執行，並先把 <STRONG_PASSWORD> 換成新密碼。
-- 建立後把帳密填入 .env 的 MYSQL_USER / MYSQL_PASSWORD，再用 check_connection 確認 account_can_write 為 false。
CREATE USER 'pjqaqc_mcp_ro'@'192.168.0.%'
    IDENTIFIED BY '<STRONG_PASSWORD>'
    WITH MAX_USER_CONNECTIONS 5 MAX_STATEMENT_TIME 15;

GRANT SELECT, SHOW VIEW ON `PJQAQC`.* TO 'pjqaqc_mcp_ro'@'192.168.0.%';
