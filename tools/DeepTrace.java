// Deep trace: find any path from callback functions to setGameState (0x2FDDF8)
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import java.util.*;

public class DeepTrace extends GhidraScript {
    private Set<Long> visited = new HashSet<>();

    @Override
    public void run() throws Exception {
        long target = 0x2FDDF8;
        // Check all functions reachable from each callback
        long[] callbacks = {
            0x2FD1B8, 0x2FD3C0, 0x2FD450, 0x2FD4F0,
            0x2FD580, 0x2FD5A0, 0x2FD7E8, 0x2FD808,
            0x2FD930, 0x2FD978, 0x2FDA68, 0x2FDB48,
            0x2FDC88, 0x2FDCB0, 0x2FDCE8
        };

        for (long cb : callbacks) {
            visited.clear();
            List<String> path = new ArrayList<>();
            if (canReach(cb, target, 6, path)) {
                println("FOUND PATH from 0x" + Long.toHexString(cb) + " to setGameState:");
                println("  " + String.join(" -> ", path));
            }
        }

        // Also check from the module dispatch functions
        long[] dispatchers = {0x308958, 0x308BA8, 0x308C08, 0x2FEA30, 0x2FE0C0};
        for (long d : dispatchers) {
            visited.clear();
            List<String> path = new ArrayList<>();
            if (canReach(d, target, 6, path)) {
                println("FOUND PATH from 0x" + Long.toHexString(d) + " to setGameState:");
                println("  " + String.join(" -> ", path));
            }
        }

        // Last resort: find ALL functions that call setGameState at any depth
        println("\n=== All functions reachable from setGameState (reverse) ===");
        Function sgsFn = getFunctionAt(toAddr(target));
        if (sgsFn != null) {
            Set<Long> callers = new HashSet<>();
            findAllCallers(sgsFn, callers, 5);
            for (Long c : callers) {
                Function f = getFunctionAt(toAddr(c));
                if (f != null) println("  " + f.getName() + " @ 0x" + Long.toHexString(c));
            }
        }

        println("\n=== Done ===");
    }

    private boolean canReach(long from, long target, int depth, List<String> path) {
        if (depth <= 0) return false;
        if (from == target) {
            path.add("0x" + Long.toHexString(from));
            return true;
        }
        if (visited.contains(from)) return false;
        visited.add(from);

        Function fn = getFunctionAt(toAddr(from));
        if (fn == null) return false;

        path.add("0x" + Long.toHexString(from));
        for (Function callee : fn.getCalledFunctions(null)) {
            if (canReach(callee.getEntryPoint().getOffset(), target, depth - 1, path)) {
                return true;
            }
        }
        path.remove(path.size() - 1);
        return false;
    }

    private void findAllCallers(Function fn, Set<Long> result, int depth) {
        if (depth <= 0) return;
        result.add(fn.getEntryPoint().getOffset());
        for (Function caller : fn.getCallingFunctions(null)) {
            if (!result.contains(caller.getEntryPoint().getOffset())) {
                findAllCallers(caller, result, depth - 1);
            }
        }
    }
}
