@Library(['aap-jenkins-shared-library@galaxy_ng_yolo']) _

pipeline {
        agent {
            kubernetes {
                yaml libraryResource('pod_templates/unpriv-ansible-pod.yaml')
            }
        }
        options {
            ansiColor('xterm')
            timestamps()
            timeout(time: 18, unit: 'HOURS')
            buildDiscarder(logRotator(daysToKeepStr: '10', numToKeepStr: '50', artifactNumToKeepStr: '40'))
        }
        /*
        parameters {
            separator(name: 'INSTALLER_VERSION_SEPARATOR', sectionHeader: 'Installer version')

            choice(
                name: 'AAP_VERSION_VAR_FILE',
                description: '''The version of AAP to install''',
                choices: AapqaProvisionerParameters.AAP_VERSIONS_VAR_FILES
            )

            separator(name: 'GALAXY_NG_SEPARATOR', sectionHeader: 'GALAXY_NG')
            string(
                name: 'GALAXY_NG_GIT_FORK',
                description: "The galaxy_ng fork",
                defaultValue: 'ansible'
            )
            string(
                name: 'GALAXY_NG_GIT_VERSION',
                description: "The galaxy_ng branch",
                defaultValue: 'master'
            )
            separator(name: 'VERSIONS_SEPARATOR', sectionHeader: 'DEPENDENCY VERSIONS')
            choice(
                name: 'VERSIONS',
                description: 'Use pulpcore, pulp_ansible, pulp-container versions defined in setup.py from galaxy_ng repo or in the installer or user-defined below.',
                choices: ["setup.py", "installer", "custom"]
            )
            string(
                name: 'AUTOMATIONHUB_PULPCORE_VERSION',
                description: '🚧Development🚧<br>Overrides "pulpcore_version" in the installer, which can pip install the right version. Only if custom has been selected.',
                defaultValue: ''
            )
            string(
                name: 'AUTOMATIONHUB_PULP_ANSIBLE_VERSION',
                description: '🚧Development🚧<br>Overrides "automationhub_pulp_ansible_version" in the installer, which can pip install the right version. Only if custom has been selected.',
                defaultValue: ''
            )
            string(
                name: 'AUTOMATIONHUB_PULP_CONTAINER_VERSION',
                description: '🚧Development🚧<br>Overrides "automationhub_pulp_container_version" in the installer, which can pip install the right version. Only if custom has been selected.',
                defaultValue: ''
            )
            string(
                name: 'AUTOMATIONHUB_UI_DOWNLOAD_URL',
                description: "The automationhub ui url",
                defaultValue: 'https://github.com/ansible/ansible-hub-ui/releases/download/dev/automation-hub-ui-dist.tar.gz'
            )
        }
        */

        stages {
            stage('Validate') {
                steps {
                    script {
                        validateInfo = stepsFactory.yoloSteps.validateYoloParameters(params)

                        List provisionFlags = []

                        installerFlags.add('input/install/flags/automationhub_content_signing.yml')
                        installerFlags.add('input/install/flags/automationhub_routable_hostname.yml')
                        installerFlags.add('input/install/flags/automationhub_from_git.yml')

                        provisionFlags.add('input/provisioner/flags/domain.yml')
                        provisionFlags.add("input/provisioner/architecture/x86_64.yml")
                        provisionFlags.add('input/provisioner/flags/domain.yml')

                        validateInfo.put("provisionFlags", provisionFlags)
                        validateInfo.put("installerFlags", installerFlags)
                    }
                }
            }

            stage('Checkout galaxy_ng repo') {
                steps {
                    container('aapqa-ansible') {
                        script {
                                stepsFactory.commonSteps.checkoutGalaxyNG([galaxyNGBranch: params.GALAXY_NG_GIT_VERSION,  galaxyNGFork: params.GALAXY_NG_GIT_FORK])
                        }
                    }
                }
            }


            stage('Get pulpcore, pulp_ansible, pulp-container versions from setup.py') {
                when {
                    expression { return params.VERSIONS == 'setup.py' }
                }

                steps {
                    container('aapqa-ansible') {
                        script {
                            def setupPyContent = readFile('setup.py').trim()
                            def lines = setupPyContent.split('\n')
                            def dependenciesToExtract = ["pulpcore", "pulp_ansible", "pulp-container"]
                            def minimumVersions = [:]
                            lines.each { line ->
                                dependenciesToExtract.each { dependency ->
                                    if (line.contains("$dependency>=")) {
                                        def versionMatch = line =~ /$dependency>=([\d.]+)/
                                        if (versionMatch) {
                                            minimumVersions[dependency] = versionMatch[0][1]
                                        }
                                    }
                                }
                            }

                            dependenciesToExtract.each { dependency ->
                                if (minimumVersions.containsKey(dependency)) {
                                    println("Using $dependency version: ${minimumVersions[dependency]}")
                                } else {
                                    println("$dependency not found in setup.py. Using version defined in the installer")
                                }
                            }
                            if (minimumVersions.containsKey("pulpcore")){
                                pulpcore_version = minimumVersions["pulpcore"]
                            } 
                            if (minimumVersions.containsKey("pulp_ansible")){
                                automationhub_pulp_ansible_version = minimumVersions["pulp_ansible"]
                            }
                            if (minimumVersions.containsKey("pulp-container")){
                                automationhub_pulp_container_version = minimumVersions["pulp-container"]
                            }
                        }
                    }
                }
                
            }

            stage('Get pulpcore, pulp_ansible, pulp-container versions from Jenkins parameters') {
                when {
                    expression { return params.VERSIONS == 'custom' }
                }

                steps {
                    container('aapqa-ansible') {
                        script {
                                pulpcore_version = params.AUTOMATIONHUB_PULPCORE_VERSION
                                automationhub_pulp_ansible_version = params.AUTOMATIONHUB_PULP_ANSIBLE_VERSION
                                automationhub_pulp_container_version = params.AUTOMATIONHUB_PULP_CONTAINER_VERSION
                            }
                        }
                   }
            } 

            stage('Setup aapqa-provisioner') {
                steps {
                    container('aapqa-ansible') {
                        script {
                            stepsFactory.aapqaSetupSteps.setup()
                        }
                    }
                }
            }

            stage('Provision') {
                steps {
                    container('aapqa-ansible') {
                        script {
                            provisionInfo = [
                                    provisionerPrefix: validateInfo.provisionPrefix,
                                    cloudVarFile     : "input/provisioner/cloud/aws.yml",
                                    scenarioVarFile  : "input/aap_scenarios/1inst_1hybr_1ahub.yml",
                            ]
                            provisionInfo = stepsFactory.aapqaOnPremProvisionerSteps.provision(provisionInfo + [
                                    provisionerVarFiles: validateInfo.get("provisionFlags") + [
                                            "input/platform/rhel88.yml",
                                    ],
                                    isPermanentDeploy  : false,
                                    registerWithRhsm   : true,
                                    runMeshScalingTests: false,
                                    runInstallerTests  : false
                            ])
                        }
                    }
                }
                post {
                    always {
                        script {
                            stepsFactory.aapqaOnPremProvisionerSteps.archiveArtifacts()
                        }
                    }
                }
            }
            
            stage('Install') {
                steps {
                    container('aapqa-ansible') {
                        script {
                            installerFlags = validateInfo.get("installerFlags")
      
                            installerVars = [:]

                            Map ahubPipParams = [
                                    automationhub_git_url: "https://github.com/${GALAXY_NG_GIT_FORK}/galaxy_ng",
                                    automationhub_git_version: "${params.GALAXY_NG_GIT_VERSION}",
                                    automationhub_ui_download_url: "${params.AUTOMATIONHUB_UI_DOWNLOAD_URL}",
                            ]
                            if (pulpcore_version != '') {
                                ahubPipParams['pulpcore_version'] = "${pulpcore_version}"
                                println("Using pulpcore version: ${pulpcore_version}")
                            }else{
                                println("pulpcore_version version not provided, using version defined in the installer")
                            }
                            if (automationhub_pulp_ansible_version != '') {
                                ahubPipParams['automationhub_pulp_ansible_version'] = "${automationhub_pulp_ansible_version}"
                                println("Using pulp_ansible version: ${automationhub_pulp_ansible_version}")
                            }else{
                                println("pulp_ansible version not provided, using version defined in the installer")
                            }
                            if (automationhub_pulp_container_version != '') {
                                ahubPipParams['automationhub_pulp_container_version'] = "${automationhub_pulp_container_version}"
                                println("Using pulp-container version: ${automationhub_pulp_container_version}")
                            }else{
                                println("pulp-container version not provided, using version defined in the installer")
                            }

                            writeYaml(
                                    file: 'input/install/ahub_pip.yml',
                                    data: ahubPipParams
                            )
                            installerFlags.add('input/install/ahub_pip.yml')
                            archiveArtifacts(artifacts: 'input/install/ahub_pip.yml')
                            
                            installInfo = stepsFactory.aapqaAapInstallerSteps.install(provisionInfo + [
                                aapVersionVarFile: "${params.AAP_VERSION_VAR_FILE}",
                                installerVarFiles: installerFlags + [
                                    "input/aap_scenarios/1inst_1hybr_1ahub.yml",
                                    "input/platform/rhel88.yml"
                                ],
                                installerVars: installerVars
                            ])
                        }
                    }
                }

                post {
                    always {
                        script {
                            container('aapqa-ansible') {
                                stepsFactory.aapqaAapInstallerSteps.collectAapInstallerArtifacts(provisionInfo + [
                                        archiveArtifactsSubdir: 'install'
                                ])

                                if (fileExists('artifacts/install/setup.log')) {
                                    sh """
                                        echo "Install setup log:"
                                        echo "-------------------------------------------------"
                                        cat artifacts/install/setup.log
                                        echo "-------------------------------------------------"
                                    """
                                }
                            }
                        }
                    }
                }
            }

            stage('Run AutomationHub Tests') {
                steps {
                    container('aapqa-ansible') {
                        script {

                            stepsFactory.aapqaAutomationHubSteps.setup(installInfo + [galaxyNgFork: "chr-stian", galaxyNgBranch: "installer_smoke_test"])
                            stepsFactory.aapqaAutomationHubSteps.runAutomationHubSuite(installInfo + [ahubTestExpression: "installer_smoke_test"])
                            stepsFactory.commonSteps.saveXUnitResultsToJenkins(xunitFile: 'ah-results.xml')
                            stepsFactory.aapqaAutomationHubSteps.reportTestResults(provisionInfo + installInfo +
                                    [
                                            component: 'ahub',
                                            testType: 'api',
                                    ], "ah-results.xml")
                        }
                    }
                }
                post {
                    always {
                        container('aapqa-ansible') {
                            script {
                                stepsFactory.aapqaAutomationHubSteps.cleanup(installInfo)
                            }
                        }
                    }
                }
            }
            
        }

        post {
            always {
                container('aapqa-ansible') {
                    script {
                        stepsFactory.aapqaAapInstallerSteps.generateAndCollectSosReports(provisionInfo)
                    }
                }
            }
            cleanup {
                container('aapqa-ansible') {
                    script {
                        if (provisionInfo != [:]) {
                            stepsFactory.aapqaOnPremProvisionerSteps.cleanup(provisionInfo)
                        }
                    }
                }
            }
        }
    }
