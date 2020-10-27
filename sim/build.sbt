import Tests._

// This needs to stay in sync with the chisel3 and firrtl git submodules
val chiselVersion = "3.4.1"

// This is set by CI and should otherwise be unmodified
val apiDirectory = settingKey[String]("The site directory into which the published scaladoc should placed.")
apiDirectory := "latest"

def filenames(tempDir: File, fs: Seq[File]): Seq[String] =
for(f <- fs) yield {
  sbtassembly.AssemblyUtils.sourceOfFileForMerge(tempDir, f) match {
    case (path, base, subDirPath, false) => subDirPath
    case (jar, base, subJarPath, true) => jar + ":" + subJarPath
  }
}

// custom sbtassemblly.MergeStrategy
// removes merge candidates that have 'rocketchip' in the name of their JAR
// hopefully leaving only a single candidate to be chosen as the member of 
// our assembly JAR.  If there's still multiple candidates, error.
val notRocketMergeStrategy = new sbtassembly.MergeStrategy {
  val name = "notRocket"
  def apply(tempDir: File, path: String, files: Seq[File]) = {
    val filtered = files collect { f =>
      sbtassembly.AssemblyUtils.sourceOfFileForMerge(tempDir, f) match {
        case (jar, _, _, true) if !jar.toString.contains("rocketchip") => f
      }
    }
    if (filtered.size == 1) Right(Seq(filtered.head -> path))
    else Left("still have multiple files after removing rocketchip for same target path:" +
      filenames(tempDir, filtered).mkString("\n", "\n", "")
      )
  }
}

// custom sbtassemblly.MergeStrategy
// keeps merge candidates that have 'rocketchip' in the name of their JAR
// hopefully leaving only a single candidate to be chosen as the member of 
// our assembly JAR.  If there's still multiple candidates, error.
val useRocketMergeStrategy = new sbtassembly.MergeStrategy {
  val name = "useRocket"
  def apply(tempDir: File, path: String, files: Seq[File]) = {
    val filtered = files collect { f =>
      sbtassembly.AssemblyUtils.sourceOfFileForMerge(tempDir, f) match {
        case (jar, _, _, true) if jar.toString.contains("rocketchip") => f
      }
    }
    if (filtered.size == 1) Right(Seq(filtered.head -> path))
    else Left("multiple candidates have rocketchip in their jar name for target:" +
      filenames(tempDir, filtered).mkString("\n", "\n", "")
      )
  }
}


lazy val commonSettings = Seq(
  organization := "berkeley",
  version      := "1.0",
  scalaVersion := "2.12.10",
  scalacOptions ++= Seq("-deprecation","-unchecked","-Xsource:2.11"),
  libraryDependencies += "org.scalatest" %% "scalatest" % "3.2.2" % "test",
  libraryDependencies += "org.json4s" %% "json4s-native" % "3.6.10",
  libraryDependencies += "org.scala-lang" % "scala-reflect" % scalaVersion.value,
  addCompilerPlugin("org.scalamacros" % "paradise" % "2.1.1" cross CrossVersion.full),
  // ScalaDoc
  autoAPIMappings  := true,
  exportJars := true,
  resolvers ++= Seq(
    Resolver.sonatypeRepo("snapshots"),
    Resolver.sonatypeRepo("releases"),
    Resolver.mavenLocal),
  assemblyMergeStrategy in assembly := {
    case PathList("META-INF", "services", xs @ _*) => MergeStrategy.concat
    // Discard Metadata, it's irrelevant
    case PathList("META-INF", xs @ _*) => MergeStrategy.discard
    // When any of our dependencies are different versions than those of firrtl.jar, there will be conflicts
    // When this occurs, pick last one which is stuff in .ivy2 (ie. not firrtl.jar)
    case PathList(xs @ _*) if xs.last.endsWith(".class") || xs.last.endsWith(".properties") => MergeStrategy.last
    // Just take the last matching joda/time/tz/data resource files
    case PathList("org", "joda", "time", "tz", "data", xs @ _*) => MergeStrategy.last
    // Use the file that doesn't come from rocketchip because we've overridden it
    case PathList(xs @ _*) if xs.last.equals("emulator.cc") => notRocketMergeStrategy
    case PathList("vsrc", "SimDTM.v") => useRocketMergeStrategy
    case x =>
      val oldStrategy = (assemblyMergeStrategy in assembly).value
      oldStrategy(x)
  },
  test in assembly := {}
)

// Fork each scala test for now, to work around persistent mutable state
// in Rocket-Chip based generators
def isolateAllTests(tests: Seq[TestDefinition]) = tests map { test =>
      val options = ForkOptions()
      new Group(test.name, Seq(test), SubProcess(options))
  } toSeq

lazy val firesimAsLibrary = sys.env.get("FIRESIM_STANDALONE") == None

lazy val chipyardDir = if(firesimAsLibrary) {
  file("../../../")
} else {
  file("../target-design/chipyard")
}

lazy val chipyard      = ProjectRef(chipyardDir, "chipyard")
lazy val chisel        = ProjectRef(workspaceDirectory / "chisel3", "chisel")
lazy val firrtl        = ProjectRef(workspaceDirectory / "firrtl", "firrtl")
lazy val rocketchip    = ProjectRef(chipyardDir, "rocketchip")
lazy val barstools     = ProjectRef(chipyardDir, "barstoolsMacros")
lazy val icenet        = ProjectRef(chipyardDir, "icenet")
lazy val testchipip    = ProjectRef(chipyardDir, "testchipip")
lazy val sifive_blocks = ProjectRef(chipyardDir, "sifive_blocks")
lazy val firechip      = ProjectRef(chipyardDir, "firechip")

// While not built from source, *must* be in sync with the chisel3 git submodule
// Building from source requires extending sbt-sriracha or a similar plugin and
//   keeping scalaVersion in sync with chisel3 to the minor version
lazy val chiselPluginLib = "edu.berkeley.cs" % "chisel3-plugin" % chiselVersion cross CrossVersion.full

lazy val targetutils   = (project in file("midas/targetutils"))
  .settings(commonSettings)
  .dependsOn(chisel)

// We cannot forward reference firesim from midas (this creates a circular
// dependency on the project definitions), so declare a reference to it
// first and use that to append to our RuntimeClasspath
lazy val firesimRef = ProjectRef(file("."), "firesim")

lazy val midas = (project in file("midas"))
  .dependsOn(barstools, rocketchip, firrtl % "test->test")
  .settings(
    commonSettings,
    addCompilerPlugin(chiselPluginLib)
  )

lazy val firesimLib = (project in file("firesim-lib"))
  .dependsOn(midas, icenet, testchipip, sifive_blocks)
  .settings(
    commonSettings,
    addCompilerPlugin(chiselPluginLib)
  )

// Contains example targets, like the MIDAS examples, and FASED tests
lazy val firesim    = (project in file("."))
  .enablePlugins(ScalaUnidocPlugin, GhpagesPlugin, SiteScaladocPlugin)
  .settings(commonSettings,
    addCompilerPlugin(chiselPluginLib),
    git.remoteRepo := "git@github.com:firesim/firesim.git",
    // Publish scala doc only for the library projects -- classes under this
    // project are all integration test-related
    unidocProjectFilter in (ScalaUnidoc, unidoc) := inProjects(targetutils, midas, firesimLib),
    siteSubdirName in ScalaUnidoc := apiDirectory.value + "/api",
    // Only delete the files in the docs branch that are in the directory were
    // trying to publish to.  This prevents dev-versions from blowing away
    // tagged versions and vice versa
    includeFilter in ghpagesCleanSite := new sbt.io.PrefixFilter(apiDirectory.value),
    excludeFilter in ghpagesCleanSite := NothingFilter,

    // Clobber the existing doc task to instead have it use the unified one
    Compile / doc := (doc in ScalaUnidoc).value,
    // Registers the unidoc-generated html with sbt-site
    addMappingsToSiteDir(mappings in (ScalaUnidoc, packageDoc), siteSubdirName in ScalaUnidoc),
    concurrentRestrictions += Tags.limit(Tags.Test, 1),
    mainClass in assembly := Some("chipyard.Generator"),
    assemblyOutputPath in assembly := file("generated-src/firesim.jar"),
  )
  .dependsOn(chisel, rocketchip, midas, firesimLib % "test->test;compile->compile", chipyard)
