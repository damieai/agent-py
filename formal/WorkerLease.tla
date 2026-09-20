-------------------------- MODULE WorkerLease --------------------------
EXTENDS Naturals
CONSTANTS FenceDispatch, FenceRelease, FenceCompletion
Workers == {"w1", "w2"}
VARIABLES epoch, owner, valid, held, pending, staleDispatch, wrongRelease, staleCompletion
vars == <<epoch, owner, valid, held, pending, staleDispatch, wrongRelease, staleCompletion>>
Init == /\ epoch = 0 /\ owner = "none" /\ valid = FALSE
        /\ held = [w \in Workers |-> 0]
        /\ pending = [w \in Workers |-> 0]
        /\ staleDispatch = FALSE /\ wrongRelease = FALSE /\ staleCompletion = FALSE
Current(w, token) == valid /\ owner = w /\ token = epoch
Acquire(w) == /\ ~valid /\ epoch < 2
              /\ epoch' = epoch + 1 /\ owner' = w /\ valid' = TRUE
              /\ held' = [held EXCEPT ![w] = epoch + 1]
              /\ UNCHANGED <<pending, staleDispatch, wrongRelease, staleCompletion>>
Expire == /\ valid /\ valid' = FALSE
          /\ UNCHANGED <<epoch, owner, held, pending, staleDispatch, wrongRelease, staleCompletion>>
Dispatch(w) == /\ held[w] > 0 /\ pending[w] = 0
               /\ (~FenceDispatch \/ Current(w, held[w]))
               /\ pending' = [pending EXCEPT ![w] = held[w]]
               /\ staleDispatch' = (staleDispatch \/ ~Current(w, held[w]))
               /\ UNCHANGED <<epoch, owner, valid, held, wrongRelease, staleCompletion>>
Release(w) == /\ held[w] > 0 /\ valid
              /\ (~FenceRelease \/ (owner = w /\ held[w] = epoch))
              /\ valid' = FALSE
              /\ wrongRelease' = (wrongRelease \/ ~(owner = w /\ held[w] = epoch))
              /\ UNCHANGED <<epoch, owner, held, pending, staleDispatch, staleCompletion>>
Complete(w) == /\ pending[w] > 0
               /\ (~FenceCompletion \/ Current(w, pending[w]))
               /\ staleCompletion' = (staleCompletion \/ ~Current(w, pending[w]))
               /\ pending' = [pending EXCEPT ![w] = 0]
               /\ UNCHANGED <<epoch, owner, valid, held, staleDispatch, wrongRelease>>
Next == Expire \/ (\E w \in Workers: Acquire(w) \/ Dispatch(w) \/ Release(w) \/ Complete(w))
Spec == Init /\ [][Next]_vars
TypeOK == /\ epoch \in 0..2 /\ owner \in Workers \cup {"none"} /\ valid \in BOOLEAN
          /\ held \in [Workers -> 0..2] /\ pending \in [Workers -> 0..2]
          /\ staleDispatch \in BOOLEAN /\ wrongRelease \in BOOLEAN /\ staleCompletion \in BOOLEAN
NoStaleDispatch == ~staleDispatch
NoWrongRelease == ~wrongRelease
NoStaleCompletion == ~staleCompletion
=============================================================================
