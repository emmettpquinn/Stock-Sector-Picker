from dotenv import load_dotenv
from sector_investor import SectorInvestor
from monitor_market import monitor_market

load_dotenv()

if __name__ == "__main__":
    investor = SectorInvestor()
    monitor_market(investor, poll_seconds=900, max_cycles=1)
