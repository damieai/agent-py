----------------------------- MODULE Action -----------------------------
EXTENDS Naturals, TLC
CONSTANT IdempotentRemote
VARIABLES state, effects, seen, approved, cancelled
vars == <<state, effects, seen, approved, cancelled>>

Init == /\ state = "NOT_SUBMITTED" /\ effects = 0
        /\ seen = FALSE /\ approved = FALSE /\ cancelled = FALSE
Approve == /\ ~cancelled /\ approved' = TRUE
           /\ UNCHANGED <<state, effects, seen, cancelled>>
Dispatch == /\ state = "NOT_SUBMITTED" /\ approved /\ ~cancelled
            /\ state' = "PENDING" /\ UNCHANGED <<effects, seen, approved, cancelled>>
RemoteCommit == /\ state \in {"PENDING", "UNKNOWN"} /\ effects < 2
                /\ effects' = IF IdempotentRemote /\ seen THEN effects ELSE effects + 1
                /\ seen' = TRUE /\ UNCHANGED <<state, approved, cancelled>>
LoseResponse == /\ state = "PENDING" /\ state' = "UNKNOWN"
                /\ UNCHANGED <<effects, seen, approved, cancelled>>
Confirm == /\ state \in {"PENDING", "UNKNOWN"} /\ seen
           /\ state' = "SUCCEEDED" /\ UNCHANGED <<effects, seen, approved, cancelled>>
Cancel == /\ cancelled' = TRUE /\ UNCHANGED <<state, effects, seen, approved>>
Next == Approve \/ Dispatch \/ RemoteCommit \/ LoseResponse \/ Confirm \/ Cancel
Spec == Init /\ [][Next]_vars
AtMostOneEffect == effects <= 1
ConfirmedHasEvidence == state = "SUCCEEDED" => seen
TypeOK == /\ state \in {"NOT_SUBMITTED", "PENDING", "UNKNOWN", "SUCCEEDED"}
          /\ effects \in 0..2 /\ seen \in BOOLEAN /\ approved \in BOOLEAN /\ cancelled \in BOOLEAN
=============================================================================
