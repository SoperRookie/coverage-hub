package covhub.agent;

import java.io.BufferedOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * covhub 的薄 agent：替 JaCoCo agent 把数据送到 hub 的 push 收集端。
 *
 * <p>为什么不直接用 JaCoCo 的 output=tcpclient —— 它有两个改不了的行为：
 * <ul>
 * <li>启动时连不上收集端，premain 直接抛异常，<b>被测 JVM 起不来</b>；</li>
 * <li>连接断了（hub 重启）之后<b>不会重连</b>，这个进程此后的覆盖率再也取不到。</li>
 * </ul>
 * 所以 JaCoCo agent 改挂 output=none（只插桩、不联网），由本类在后台线程里连 hub、
 * 断了就重连。数据经 JaCoCo 的公开入口 {@code org.jacoco.agent.rt.RT} 取，线上说的仍是
 * JaCoCo 的 remote control 协议 —— hub 那头分不出对面是 tcpclient 还是本类，JaCoCo
 * 源码也一行不用改。
 *
 * <p>几条硬约束：
 * <ul>
 * <li><b>任何情况下都不能影响被测应用</b>：premain 不抛异常，工作线程是 daemon，
 * 所有失败只打一行日志。</li>
 * <li>对 JaCoCo <b>只用反射</b>：编译不依赖 jacocoagent.jar，换 JaCoCo 版本不用重编。
 * 方法要从公开接口 IAgent 上取 —— 实现类在 internal_xxx 包里，包名随版本变。</li>
 * <li><b>只有这一个类</b>、不用 lambda / 内部类，目标字节码 Java 8，零依赖。</li>
 * <li>日志用英文：被测容器里 stderr 的编码常常是 POSIX，中文会变成一串问号。</li>
 * </ul>
 *
 * <p>参数（{@code -javaagent:covhub-agent.jar=address=hub,port=6400,idle=900}）：
 * address 必填；port 默认 6400；idle 是「多少秒没收到 hub 的指令就当连接已死、重连」，
 * 0 表示不判。idle 防的是收不到 FIN 的断线（hub 那台机器掉电、中间的 NAT 把空闲连接
 * 悄悄回收）—— 那种情况下读会永远阻塞，光靠断线重连救不回来。
 */
public final class CovhubAgent implements Runnable {

	static final String VERSION = "1.0";

	// 与 covhub/exec_format.py 同一组常量：块类型 + magic 0xC0C0 + 格式版本 0x1007
	private static final int BLOCK_HEADER = 0x01;
	private static final int BLOCK_CMDOK = 0x20;
	private static final int BLOCK_CMDDUMP = 0x40;
	private static final byte[] HEADER = { BLOCK_HEADER, (byte) 0xC0,
			(byte) 0xC0, 0x10, 0x07 };

	private static final int CONNECT_TIMEOUT_MS = 5000;
	private static final long RETRY_MIN_MS = 1000;
	private static final long RETRY_MAX_MS = 30000;
	// JaCoCo agent 正常在本类之前就绪；等这么久还没有，说明它根本没挂
	private static final int JACOCO_WAIT_SECONDS = 60;

	private static final AtomicBoolean STARTED = new AtomicBoolean();

	private final String address;
	private final int port;
	private final int idleSeconds;

	private Object jacoco;
	private Method getExecutionData;
	private Method reset;

	private CovhubAgent(final String address, final int port,
			final int idleSeconds) {
		this.address = address;
		this.port = port;
		this.idleSeconds = idleSeconds;
	}

	public static void premain(final String args, final Instrumentation inst) {
		try {
			start(args);
		} catch (final Throwable t) {
			// 挂 agent 是为了测覆盖率，不是为了让服务起不来
			log("not started: " + t);
		}
	}

	public static void main(final String[] args) {
		System.out.println("covhub-agent " + VERSION);
		System.out.println("usage: -javaagent:jacocoagent.jar=output=none,... "
				+ "-javaagent:covhub-agent.jar=address=<hub>[,port=6400][,idle=<seconds>]");
	}

	private static void start(final String args) {
		String address = null;
		int port = 6400;
		int idle = 0;
		for (final String item : (args == null ? "" : args).split(",")) {
			final int eq = item.indexOf('=');
			if (eq < 0) {
				continue;
			}
			final String key = item.substring(0, eq).trim();
			final String value = item.substring(eq + 1).trim();
			if ("address".equals(key)) {
				address = value;
			} else if ("port".equals(key)) {
				port = Integer.parseInt(value);
			} else if ("idle".equals(key)) {
				idle = Integer.parseInt(value);
			} else {
				log("unknown option ignored: " + key);
			}
		}
		if (address == null || address.length() == 0) {
			log("not started: option 'address' (hub collector address) is required");
			return;
		}
		// JAVA_TOOL_OPTIONS 和启动命令里各挂了一次时，只起一条连接
		if (!STARTED.compareAndSet(false, true)) {
			return;
		}
		final Thread thread = new Thread(
				new CovhubAgent(address, port, Math.max(idle, 0)),
				"covhub-agent");
		thread.setDaemon(true);
		thread.start();
	}

	public void run() {
		try {
			if (!findJacoco()) {
				return;
			}
			log("started (" + VERSION + "), collector " + address + ":" + port);
			connectLoop();
		} catch (final InterruptedException e) {
			// JVM 在收尾，安静退出
		} catch (final Throwable t) {
			log("stopped unexpectedly: " + t);
		}
	}

	/**
	 * 取到 JaCoCo 的 IAgent 实例。两个 -javaagent 的先后不该成为接入的坑，所以
	 * 「还没启动」等一等再试；类都找不到才是真的没挂。
	 */
	private boolean findJacoco() throws Exception {
		final ClassLoader loader = ClassLoader.getSystemClassLoader();
		final Class<?> rt;
		final Class<?> iagent;
		try {
			rt = Class.forName("org.jacoco.agent.rt.RT", true, loader);
			iagent = Class.forName("org.jacoco.agent.rt.IAgent", true, loader);
		} catch (final ClassNotFoundException e) {
			log("not started: JaCoCo agent not found, "
					+ "-javaagent:jacocoagent.jar=output=none,... must be present as well");
			return false;
		}
		final Method getAgent = rt.getMethod("getAgent");
		for (int i = 0;; i++) {
			try {
				jacoco = getAgent.invoke(null);
				break;
			} catch (final InvocationTargetException e) {
				if (i >= JACOCO_WAIT_SECONDS) {
					log("not started: JaCoCo agent is not running ("
							+ e.getCause() + ")");
					return false;
				}
				Thread.sleep(1000);
			}
		}
		getExecutionData = iagent.getMethod("getExecutionData", boolean.class);
		reset = iagent.getMethod("reset");
		return true;
	}

	private void connectLoop() throws InterruptedException {
		long delay = RETRY_MIN_MS;
		// 连不上只在第一次说一声：hub 停机维护时不该把应用日志刷满
		boolean complained = false;
		while (true) {
			final Socket socket = new Socket();
			boolean connected = false;
			try {
				// 每次现解析地址：hub 换了 IP（K8s Service 重建）也能跟上
				socket.connect(new InetSocketAddress(address, port),
						CONNECT_TIMEOUT_MS);
				socket.setKeepAlive(true);
				socket.setTcpNoDelay(true);
				socket.setSoTimeout(idleSeconds * 1000);
				connected = true;
				complained = false;
				delay = RETRY_MIN_MS;
				log("connected to " + address + ":" + port);
				serve(socket);
				log("connection closed by collector, reconnecting");
			} catch (final SocketTimeoutException e) {
				if (connected) {
					log("no command from collector for " + idleSeconds
							+ "s, reconnecting");
				} else if (!complained) {
					complained = true;
					log("cannot reach " + address + ":" + port + " (" + e
							+ "), retrying in background");
				}
			} catch (final IOException e) {
				if (connected) {
					log("connection lost (" + e + "), reconnecting");
				} else if (!complained) {
					complained = true;
					log("cannot reach " + address + ":" + port + " (" + e
							+ "), retrying in background");
				}
			} catch (final RuntimeException e) {
				// 地址写错（UnresolvedAddressException 之类）也只是「现在连不上」
				if (!complained) {
					complained = true;
					log("cannot reach " + address + ":" + port + " (" + e
							+ "), retrying in background");
				}
			} finally {
				try {
					socket.close();
				} catch (final IOException e) {
					// 已经没用了
				}
			}
			Thread.sleep(delay);
			delay = Math.min(delay * 2, RETRY_MAX_MS);
		}
	}

	/**
	 * 在一条连接上应答 hub 的指令，直到对端关闭。行为与 JaCoCo 的 TcpConnection 一致：
	 * 连上先发 header，之后每条 dump 指令回一批数据 + CMDOK。
	 */
	private void serve(final Socket socket) throws IOException {
		final OutputStream out = new BufferedOutputStream(
				socket.getOutputStream());
		final InputStream in = socket.getInputStream();
		out.write(HEADER);
		out.flush();
		while (true) {
			final int type = in.read();
			if (type < 0) {
				return;
			}
			switch (type) {
			case BLOCK_HEADER:
				// hub 每次发指令前都带一个 header，不只是连接开头那一次
				if (readByte(in) != 0xC0 || readByte(in) != 0xC0) {
					throw new IOException("peer is not a JaCoCo collector");
				}
				readByte(in);
				readByte(in);
				break;
			case BLOCK_CMDDUMP:
				final boolean dump = readByte(in) != 0;
				final boolean doReset = readByte(in) != 0;
				if (dump) {
					final byte[] data = executionData(doReset);
					// getExecutionData 给的是完整的 exec 流，开头带 header；连接上
					// header 已经发过，这里只送后面的 SessionInfo 与执行数据
					final int skip = data.length >= HEADER.length
							&& data[0] == BLOCK_HEADER ? HEADER.length : 0;
					out.write(data, skip, data.length - skip);
				} else if (doReset) {
					invoke(reset);
				}
				out.write(BLOCK_CMDOK);
				out.flush();
				break;
			default:
				throw new IOException("unknown block type 0x"
						+ Integer.toHexString(type));
			}
		}
	}

	private static int readByte(final InputStream in) throws IOException {
		final int b = in.read();
		if (b < 0) {
			throw new EOFException("collector closed the connection mid-command");
		}
		return b;
	}

	private byte[] executionData(final boolean doReset) throws IOException {
		return (byte[]) invoke(getExecutionData, Boolean.valueOf(doReset));
	}

	private Object invoke(final Method method, final Object... args)
			throws IOException {
		try {
			return method.invoke(jacoco, args);
		} catch (final InvocationTargetException e) {
			throw new IOException("JaCoCo agent failed: " + e.getCause());
		} catch (final IllegalAccessException e) {
			throw new IOException("JaCoCo agent not accessible: " + e);
		}
	}

	private static void log(final String message) {
		System.err.println("[covhub-agent] " + message);
	}
}
