from pydantic import BaseModel
from typing import Literal, Optional, List
from datetime import datetime

class TradeSignal(BaseModel):
    ticker:             str
    action:             Literal["BUY","SELL","HOLD"]
    direction:          Literal["LONG","SHORT","NONE"]
    confidence:         float          # 0.0 - 1.0
    conviction:         Literal["LOW","MED","HIGH"]
    entry_price:        float
    stop_loss:          float
    take_profit:        float
    contract_type:      Literal["CALL","PUT","NONE"] = "NONE"
    dte_preference:     int = 1        # 0=0DTE, 1=1DTE, etc
    rationale:          str
    indicators_used:    List[str] = []
    bull_score:         float = 0.0
    bear_score:         float = 0.0
    gex_aligned:        bool = False
    flow_confirmed:     bool = False
    similar_past_signals: Optional[List[dict]] = []
    generated_at:       Optional[datetime] = None

    def model_post_init(self, __context):
        if self.generated_at is None:
            self.generated_at = datetime.now()

class ExecutionResult(BaseModel):
    signal:         TradeSignal
    order_id:       str = ""
    contract_sym:   str = ""
    filled_price:   float = 0.0
    qty:            int = 0
    status:         str = "pending"
    error:          str = ""
    executed_at:    Optional[datetime] = None
    pnl:            Optional[float] = None
    exit_price:     Optional[float] = None
    exit_time:      Optional[str] = None

    def model_post_init(self, __context):
        if self.executed_at is None:
            self.executed_at = datetime.now()
